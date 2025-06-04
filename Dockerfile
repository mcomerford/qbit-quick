FROM python:3.12-slim

RUN pip install poetry

WORKDIR /app

COPY pyproject.toml poetry.lock README.md ./

RUN poetry install --no-root --without dev

COPY src/qbitquick ./qbitquick

ENTRYPOINT ["poetry", "run", "python", "-m", "qbitquick.qbit_quick"]