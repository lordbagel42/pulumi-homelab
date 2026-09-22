FROM ubuntu:24.04

# Asterisk + build toolchain for chan-sccp-b (Skinny/SCCP channel driver).
# The Cisco 7945G runs SCCP firmware, so we speak SCCP natively instead of
# reflashing it to SIP. Asterisk lives in Ubuntu's "universe" component.
ENV DEBIAN_FRONTEND=noninteractive
RUN sed -i 's/^Components: main$/Components: main universe/' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
        asterisk asterisk-dev asterisk-modules \
        asterisk-core-sounds-en-gsm asterisk-core-sounds-en-g722 \
        build-essential git automake autoconf libtool pkg-config \
        libncurses-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG SCCP_REF=develop
RUN git clone --depth 1 --branch ${SCCP_REF} https://github.com/chan-sccp/chan-sccp.git /src/chan-sccp
WORKDIR /src/chan-sccp
RUN ./configure --enable-conference --enable-advanced-functions \
    && make -j4 \
    && make install

RUN mkdir -p /var/run/asterisk /var/log/asterisk /var/spool/asterisk

# Asterisk 20.6's AudioSocket application omits keypad events. This source
# preserves that version's implementation and adds protocol 0x03 DTMF frames.
COPY app_audiosocket.c /src/app_audiosocket-phone.c
RUN apt-get update && apt-get install -y --no-install-recommends uuid-dev \
    && rm -rf /var/lib/apt/lists/* \
    && cc -shared -fPIC '-DAST_MODULE="app_audiosocket"' \
    -DAST_MODULE_SELF_SYM=__internal_app_audiosocket_self \
    -o /usr/lib/x86_64-linux-gnu/asterisk/modules/app_audiosocket.so /src/app_audiosocket-phone.c -luuid

EXPOSE 2000/tcp
CMD ["asterisk", "-f", "-vvvg", "-C", "/etc/asterisk/asterisk.conf"]
