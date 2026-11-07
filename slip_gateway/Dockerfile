FROM python:3.12-alpine

RUN apk add --no-cache iproute2 \
 && pip install --no-cache-dir pyserial zeroconf

COPY slipgw.py /slipgw.py

CMD ["python3", "-u", "/slipgw.py"]
