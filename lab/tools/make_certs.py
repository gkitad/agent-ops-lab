"""Creates the lab's internal CA, a valid certificate for crm.lab.internal,
and an expired one with the same key (used by the expired-certificate fault)."""

import datetime as dt
import os
import shutil
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

out = sys.argv[1] if len(sys.argv) > 1 else "/certs"
os.makedirs(os.path.join(out, "live"), exist_ok=True)
now = dt.datetime.now(dt.timezone.utc)


def name(cn):
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Lakeside Logistics"), x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def write(path, data):
    with open(os.path.join(out, path), "wb") as f:
        f.write(data)


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


def leaf(start, end):
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


write("ca.crt", ca.public_bytes(serialization.Encoding.PEM))
write("crm.key", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
write("valid.crt", leaf(now - dt.timedelta(days=30), now + dt.timedelta(days=60)))
write("expired.crt", leaf(now - dt.timedelta(days=91), now - dt.timedelta(days=1)))
shutil.copy(os.path.join(out, "valid.crt"), os.path.join(out, "live", "crm.crt"))
shutil.copy(os.path.join(out, "crm.key"), os.path.join(out, "live", "crm.key"))
os.chmod(os.path.join(out, "crm.key"), 0o644)
os.chmod(os.path.join(out, "live", "crm.key"), 0o644)
print("certificates written to", out)
