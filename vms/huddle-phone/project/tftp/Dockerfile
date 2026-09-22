FROM ubuntu:24.04

# Minimal read-only TFTP server (dnsmasq in tftp-only mode) that hands Cisco
# phones their SEP<MAC>.cnf.xml provisioning file.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends dnsmasq \
    && rm -rf /var/lib/apt/lists/*

EXPOSE 69/udp
# --port=0 disables DNS/DHCP; we only want TFTP.
CMD ["dnsmasq", "--keep-in-foreground", "--log-facility=-", \
     "--port=0", "--enable-tftp", "--tftp-root=/srv/tftp", \
     "--tftp-no-blocksize", "--user=root"]
