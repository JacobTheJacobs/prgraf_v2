FROM python:3.12-slim

WORKDIR /app

# Install git and build tools (needed for tree-sitter)
RUN apt-get update && apt-get install -y \
    git \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy application code (needed for pip install)
COPY . .

# Install dependencies and the package
RUN pip install .

# Expose API port
EXPOSE 8000

CMD ["uvicorn", "codebase_rag.api:app", "--host", "0.0.0.0", "--port", "8000"]
