"""Certificates for the lab's internal CA.

  make_certs.py /certs           create the CA, a valid certificate for
                                 crm.lab.internal and an expired one with the
                                 same key (used by the expired-certificate fault)
  make_certs.py /certs --renew   issue a fresh certificate for crm.lab.internal
                                 from the existing CA into /certs/live
"""

import datetime as dt
import os
import shutil
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

args = [a for a in sys.argv[1:] if not a.startswith("--")]
out = args[0] if args else "/certs"
renew = "--renew" in sys.argv
os.makedirs(os.path.join(out, "live"), exist_ok=True)
now = dt.datetime.now(dt.timezone.utc)


def name(cn):
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Lakeside Logistics"), x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def path(p):
    return os.path.join(out, p)


def write(p, data, mode=0o644):
    with open(path(p) + ".tmp", "wb") as f:
        f.write(data)
    os.chmod(path(p) + ".tmp", mode)
    os.replace(path(p) + ".tmp", path(p))


def pem_key(k):
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def leaf(ca, ca_key, key, start, end):
    return (
        x509.CertificateBuilder()
        .subject_name(name("crm.lab.internal"))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("crm.lab.internal")]), critical=False)
        .sign(ca_key, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )


if renew:
    with open(path("ca.crt"), "rb") as f:
        ca = x509.load_pem_x509_certificate(f.read())
    with open(path("ca.key"), "rb") as f:
        ca_key = serialization.load_pem_private_key(f.read(), None)
    with open(path("live/crm.key"), "rb") as f:
        key = serialization.load_pem_private_key(f.read(), None)
    end = now + dt.timedelta(days=90)
    write("live/crm.crt", leaf(ca, ca_key, key, now - dt.timedelta(minutes=5), end))
    print(f"issued new certificate for crm.lab.internal, valid until {end:%Y-%m-%d %H:%M} UTC")
    sys.exit(0)

ca_key = ec.generate_private_key(ec.SECP256R1())
ca = (
    x509.CertificateBuilder()
    .subject_name(name("Lakeside Internal CA"))
    .issuer_name(name("Lakeside Internal CA"))
    .public_key(ca_key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now - dt.timedelta(days=365))
    .not_valid_after(now + dt.timedelta(days=3650))
    .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
    .sign(ca_key, hashes.SHA256())
)
key = ec.generate_private_key(ec.SECP256R1())

write("ca.crt", ca.public_bytes(serialization.Encoding.PEM))
write("ca.key", pem_key(ca_key), 0o600)
write("crm.key", pem_key(key))
write("valid.crt", leaf(ca, ca_key, key, now - dt.timedelta(days=30), now + dt.timedelta(days=60)))
write("expired.crt", leaf(ca, ca_key, key, now - dt.timedelta(days=91), now - dt.timedelta(days=1)))
shutil.copy(path("valid.crt"), path("live/crm.crt"))
shutil.copy(path("crm.key"), path("live/crm.key"))
os.chmod(path("live/crm.key"), 0o644)
print("certificates written to", out)
