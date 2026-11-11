FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[docker,test]'
COPY docker ./docker
COPY tests ./tests
ENV PYTHONUNBUFFERED=1
CMD ["bench", "--help"]
