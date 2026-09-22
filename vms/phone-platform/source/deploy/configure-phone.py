#!/usr/bin/env python3
"""Apply the directory URL only to deployed phone XML, never public source."""
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlencode
import xml.etree.ElementTree as ET


def configure(root=Path("/opt/huddle-phone/current"),
              token_file=Path("/var/lib/codex-phone/switchboard/phone-token")):
    token = token_file.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token):
        raise ValueError("Invalid phone-directory credential")
    config = root / "tftp/files/SEP0C2724317F2E.cnf.xml"
    tree = ET.parse(config)
    element = tree.getroot().find("directoryURL")
    if element is None:
        element = ET.SubElement(tree.getroot(), "directoryURL")
    element.text = "http://192.168.0.233:8088/phone/directories?" + urlencode({"token": token})
    # The phone must read this via TFTP. The credential grants only directory
    # reads and is unrelated to the API/admin bearer token.
    with tempfile.NamedTemporaryFile(dir=config.parent, delete=False) as staged:
        tree.write(staged, encoding="UTF-8", xml_declaration=True)
        os.fchmod(staged.fileno(), 0o644)
    os.replace(staged.name, config)
    print("Cisco phone directory URL installed; credential omitted.")


if __name__ == "__main__":
    configure()
