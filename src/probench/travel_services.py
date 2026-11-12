"""Small policy-enforcing travel services for simulated bookings.

These services model a local inventory and payment ledger. They do not contact
airlines, hotels or payment processors. External mutations remain controller-only.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import Literal
from pydantic import BaseModel, Field

from .services import DEFAULT_REGISTRY, OperationSpec, RecordAdapter


class TicketArgs(BaseModel):
    option_id: str
    authorization_id: str


class LateArrivalArgs(BaseModel):
    reservation_id: str
    expected_arrival_at: str


class FlightInventory(BaseModel):
    id: str
    kind: Literal["inventory"]
    code: str
    origin: str
    destination: str
    departure_at: str
    arrival_at: str
    cabin: str
    quoted_fare_cny: int = Field(gt=0)
    seats_remaining: int = Field(ge=0)


class PaymentAccount(BaseModel):
    id: str
    kind: Literal["payment_account"]
    currency: Literal["CNY"]
    available_balance_cny: int = Field(ge=0)
    status: Literal["active", "suspended"]


class TicketAuthorization(BaseModel):
    id: str
    kind: Literal["authorization"]
    action: Literal["issue_ticket"]
    option_id: str
    cabin: str
    currency: Literal["CNY"]
    max_total_cny: int = Field(gt=0)
    payment_account_id: str
    status: Literal["approved", "consumed", "revoked"]
    source_user_event_id: str
    approved_at: datetime
    expires_at: datetime


class FlightBookingAdapter(RecordAdapter):
    """Atomic inventory, authorization and payment checks for a new ticket."""

    name = "flight_booking"
    operations = {
        **{key: value for key, value in RecordAdapter.operations.items() if key in ("list", "search", "get")},
        "patch": OperationSpec("patch", "Apply an external flight update.", RecordAdapter.operations["patch"].input_model, True, agent_visible=False),
        "insert": RecordAdapter.operations["insert"],
        "issue_ticket": OperationSpec("issue_ticket", "Issue an authorized ticket from current inventory and charge the travel account.", TicketArgs, True),
    }

    def validate_state(self, state):
        normalized = super().validate_state(state)
        schemas = {"inventory": FlightInventory, "payment_account": PaymentAccount, "authorization": TicketAuthorization}
        for row in normalized.values():
            schema = schemas.get(row.get("kind"))
            if schema is not None:
                schema.model_validate(row)
        return normalized

    def execute_at(self, state, operation, arguments, *, mutate, timestamp):
        if operation == "issue_ticket":
            now = datetime.fromisoformat(timestamp)
            option = state.get(arguments["option_id"], {})
            approval = state.get(arguments["authorization_id"], {})
            if approval.get("kind") != "authorization" or approval.get("status") != "approved":
                raise PermissionError("specific user authorization is required")
            if approval.get("option_id") != arguments["option_id"]:
                raise PermissionError("authorization does not cover this ticket")
            try:
                quote_at = datetime.fromisoformat(option["quote_as_of"])
                departure = datetime.fromisoformat(option["departure_at"])
                approved_at = datetime.fromisoformat(approval["approved_at"])
                expires_at = datetime.fromisoformat(approval["expires_at"])
            except (KeyError, ValueError, TypeError) as exc:
                raise ValueError("current quote and dated user authorization are required") from exc
            if any(value.tzinfo is None for value in (now, quote_at, departure, approved_at, expires_at)):
                raise ValueError("booking timestamps require timezones")
            if not (approved_at <= now <= expires_at):
                raise PermissionError("user authorization is not currently valid")
            if not (quote_at <= now <= quote_at + timedelta(hours=24)):
                raise ValueError("flight quote is stale; obtain an updated price")
            if departure <= now:
                raise ValueError("flight has already departed")
        return self.execute(state, operation, arguments, mutate=mutate)

    def execute(self, state, operation, arguments, *, mutate):
        if operation != "issue_ticket":
            return super().execute(state, operation, arguments, mutate=mutate)
        if not mutate:
            raise ValueError("ticket issuance changes world state")
        option_id, approval_id = arguments["option_id"], arguments["authorization_id"]
        option = state.get(option_id)
        approval = state.get(approval_id)
        account = state.get("travel_account")
        if not option or option.get("kind") != "inventory":
            raise ValueError("flight option is unavailable")
        if option.get("status", "available") != "available" or not isinstance(option.get("seats_remaining"), int) or option["seats_remaining"] < 1:
            raise ValueError("flight inventory is exhausted")
        fare = option.get("quoted_fare_cny")
        if not isinstance(fare, int) or fare <= 0:
            raise ValueError("flight quote has no valid fare")
        if not approval or approval.get("kind") != "authorization" or approval.get("status") != "approved":
            raise PermissionError("specific user authorization is required")
        if approval.get("action") != "issue_ticket" or approval.get("option_id") != option_id:
            raise PermissionError("authorization does not cover this ticket")
        if approval.get("cabin") != option.get("cabin") or approval.get("currency") != "CNY":
            raise PermissionError("authorization does not cover this cabin or currency")
        ceiling = approval.get("max_total_cny")
        if not isinstance(ceiling, int) or fare > ceiling:
            raise PermissionError("quoted fare exceeds the authorized amount")
        if not account or account.get("kind") != "payment_account" or account.get("status") != "active":
            raise ValueError("travel payment account is unavailable")
        if approval.get("payment_account_id") != account["id"]:
            raise PermissionError("authorization does not cover this payment account")
        balance = account.get("available_balance_cny")
        if not isinstance(balance, int) or balance < fare:
            raise ValueError("travel account has insufficient available balance")
        order_id = f"air_order_{approval_id}"
        segment_id = f"air_seg_{approval_id}"
        payment_id = f"payment_{approval_id}"
        if any(item in state for item in (order_id, segment_id, payment_id)):
            raise ValueError("authorization has already been used for issuance")
        option_before = copy.deepcopy(option)
        account_before = balance
        option["seats_remaining"] -= 1
        account["available_balance_cny"] -= fare
        approval["status"] = "consumed"
        state[segment_id] = {
            "id": segment_id, "kind": "flight_segment", "booking_id": order_id,
            "code": option["code"], "carrier": option.get("carrier"),
            "origin": option["origin"], "destination": option["destination"],
            "departure_at": option["departure_at"], "arrival_at": option["arrival_at"],
            "cabin": option["cabin"], "fare_cny": fare, "status": "ticketed",
        }
        state[order_id] = {
            "id": order_id, "kind": "booking", "status": "ticketed",
            "flight_id": segment_id, "source_option_id": option_id,
            "fare_cny": fare, "currency": "CNY", "authorization_id": approval_id,
            "payment_id": payment_id,
        }
        state[payment_id] = {
            "id": payment_id, "kind": "payment", "status": "captured",
            "booking_id": order_id, "account_id": account["id"],
            "amount_cny": fare, "currency": "CNY", "authorization_id": approval_id,
        }
        changes = {
            option_id: {"seats_remaining": option["seats_remaining"]},
            approval_id: {"status": "consumed"},
            account["id"]: {"available_balance_cny": account["available_balance_cny"]},
            order_id: copy.deepcopy(state[order_id]),
            segment_id: copy.deepcopy(state[segment_id]),
            payment_id: copy.deepcopy(state[payment_id]),
        }
        return {"order_id": order_id, "segment_id": segment_id, "payment_id": payment_id, "fare_cny": fare, "remaining_seats": option["seats_remaining"], "account_balance_before_cny": account_before}, changes


class HotelBookingAdapter(RecordAdapter):
    """Permits free late-arrival registration without arbitrary order edits."""

    name = "hotel_booking"
    operations = {
        **{key: value for key, value in RecordAdapter.operations.items() if key in ("list", "search", "get")},
        "patch": OperationSpec("patch", "Apply an external hotel update.", RecordAdapter.operations["patch"].input_model, True, agent_visible=False),
        "insert": RecordAdapter.operations["insert"],
        "register_late_arrival": OperationSpec("register_late_arrival", "Register a confirmed reservation's expected arrival, without payment or room changes.", LateArrivalArgs, True),
    }

    def execute(self, state, operation, arguments, *, mutate):
        if operation != "register_late_arrival":
            return super().execute(state, operation, arguments, mutate=mutate)
        if not mutate:
            raise ValueError("late-arrival registration changes world state")
        reservation_id = arguments["reservation_id"]
        row = state.get(reservation_id)
        if not row or row.get("kind") != "reservation" or row.get("status") != "confirmed":
            raise ValueError("confirmed hotel reservation is required")
        if row.get("late_arrival_registered") is True:
            return {"reservation_id": reservation_id, "already_registered": True}, {}
        from datetime import datetime
        try:
            arrival = datetime.fromisoformat(arguments["expected_arrival_at"])
            cutoff = datetime.fromisoformat(row["guaranteed_until"])
        except (KeyError, ValueError) as exc:
            raise ValueError("valid arrival and guarantee timestamps are required") from exc
        if arrival.tzinfo is None or cutoff.tzinfo is None or arrival.date().isoformat() != row["check_in"]:
            raise ValueError("expected arrival must be on the check-in date with a timezone")
        if arrival <= cutoff:
            raise ValueError("late-arrival registration is only needed after the guarantee cutoff")
        row["late_arrival_registered"] = True
        row["expected_arrival_at"] = arrival.isoformat()
        return {"reservation_id": reservation_id, "registered": True, "fee_cny": 0}, {reservation_id: {"late_arrival_registered": True, "expected_arrival_at": row["expected_arrival_at"]}}


class ExpenseRecordsAdapter(RecordAdapter):
    """Read-only for the Agent; finance policy and receipts are authoritative."""

    name = "expense_records"
    operations = {
        **{key: value for key, value in RecordAdapter.operations.items() if key in ("list", "search", "get")},
        "patch": OperationSpec("patch", "Apply an external finance update.", RecordAdapter.operations["patch"].input_model, True, agent_visible=False),
        "insert": RecordAdapter.operations["insert"],
    }


DEFAULT_REGISTRY.register("flight_booking", FlightBookingAdapter)
DEFAULT_REGISTRY.register("hotel_booking", HotelBookingAdapter)
DEFAULT_REGISTRY.register("expense_records", ExpenseRecordsAdapter)
