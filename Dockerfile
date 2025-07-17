FROM python:3.12-slim

WORKDIR /app

ARG WHEEL_FILE
COPY dist/${WHEEL_FILE} .

RUN pip install ${WHEEL_FILE}

ENTRYPOINT ["qbit-quick"]
CMD ["server"]