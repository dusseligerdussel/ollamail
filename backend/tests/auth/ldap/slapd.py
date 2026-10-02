"""A throw-away OpenLDAP server (slapd) for integration tests.

The server runs as a child process with a generated CA and server certificate and
listens on free local ports for ``ldap://`` (StartTLS) and ``ldaps://``. Test data is
loaded with ``slapadd`` before the start (fictitious people only). ``slapd`` comes from
the distribution package (``apt-get install slapd``); without it the tests are skipped,
unless ``OLLAMAIL_TEST_REQUIRE_LDAP=1`` (CI) turns that into an error.
"""

import datetime
import ipaddress
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

SUFFIX = "dc=example,dc=org"
PEOPLE = f"ou=people,{SUFFIX}"
GROUPS = f"ou=groups,{SUFFIX}"
SERVICE_DN = f"cn=ollamail-svc,{SUFFIX}"
SERVICE_PASSWORD = "service-secret-1"
USER_PASSWORD = "Erika's secret pw"
# A login whose DN needs escaping in filters: uid=o(brien)*
SPECIAL_LOGIN = "o(brien)*"

SCHEMA_DIR = Path("/etc/ldap/schema")
MODULE_DIRS = (Path("/usr/lib/ldap"), Path("/usr/lib/openldap"), Path("/usr/lib64/openldap"))


def find_slapd() -> tuple[str, str] | None:
    """Paths of ``slapd`` and ``slapadd``, if installed."""
    candidates = [shutil.which("slapd"), "/usr/sbin/slapd"]
    slapd = next((c for c in candidates if c and Path(c).exists()), None)
    slapadd = shutil.which("slapadd") or "/usr/sbin/slapadd"
    if slapd is None or not Path(slapadd).exists() or not SCHEMA_DIR.exists():
        return None
    return slapd, slapadd


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


KeyAndCert = tuple[ec.EllipticCurvePrivateKey, x509.Certificate]


def make_ca(common_name: str = "ollamail test CA") -> KeyAndCert:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(common_name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _server_cert(ca_key: ec.EllipticCurvePrivateKey, ca_cert: x509.Certificate) -> KeyAndCert:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name("ldap.test"))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                # Deliberately not "localhost": tests use that name for a host mismatch.
                [x509.DNSName("ldap.test"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def _person(uid: str, cn: str, *, mail: str | None, extra: str = "") -> str:
    lines = [
        f"dn: uid={_dn_value(uid)},{PEOPLE}",
        "objectClass: inetOrgPerson",
        "objectClass: extensibleObject",
        f"uid: {uid}",
        f"cn: {cn}",
        f"sn: {cn.split()[-1]}",
        f"userPassword: {USER_PASSWORD}",
    ]
    if mail:
        lines.append(f"mail: {mail}")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _dn_value(value: str) -> str:
    # RFC 4514 escaping for the few special characters used in the test data.
    return "".join(f"\\{c}" if c in ',+"\\<>;=' else c for c in value)


def _group(cn: str, members: list[str]) -> str:
    lines = [f"dn: cn={cn},{GROUPS}", "objectClass: groupOfNames", f"cn: {cn}"]
    lines += [f"member: {member}" for member in members]
    return "\n".join(lines)


def person_dn(uid: str) -> str:
    return f"uid={_dn_value(uid)},{PEOPLE}"


def group_dn(cn: str) -> str:
    return f"cn={cn},{GROUPS}"


def test_data() -> str:
    """Fictitious test directory.

    Groups: staff ∋ erika, o(brien)*; admins ∋ staff (nested), max; other ∋ max;
    cycle-a ∋ cycle-b ∋ cycle-a (cyclic nesting must not hang).
    """
    entries = [
        f"dn: {SUFFIX}\nobjectClass: dcObject\nobjectClass: organization\ndc: example\no: Example",
        f"dn: {PEOPLE}\nobjectClass: organizationalUnit\nou: people",
        f"dn: {GROUPS}\nobjectClass: organizationalUnit\nou: groups",
        (
            f"dn: {SERVICE_DN}\nobjectClass: organizationalRole\n"
            f"objectClass: simpleSecurityObject\ncn: ollamail-svc\n"
            f"userPassword: {SERVICE_PASSWORD}"
        ),
        _person(
            "erika",
            "Erika Mustermann",
            mail="Erika.Mustermann@Example.org",
            extra="displayName: Erika Mustermann\nuserAccountControl: 512",
        ),
        _person("max", "Max Mustermann", mail="max@example.org"),
        _person("jürgen", "Jürgen Beispiel", mail="juergen@example.org"),
        _person("nomail", "No Mail", mail=None),
        _person(
            "disabled",
            "Disabled User",
            mail="disabled@example.org",
            extra="userAccountControl: 514",
        ),
        _person(SPECIAL_LOGIN, "Oscar Brien", mail="oscar@example.org"),
        _person("twin", "Twin One", mail="twin1@example.org"),
        f"dn: ou=more,{PEOPLE}\nobjectClass: organizationalUnit\nou: more",
        (
            f"dn: uid=twin,ou=more,{PEOPLE}\nobjectClass: inetOrgPerson\nuid: twin\n"
            f"cn: Twin Two\nsn: Two\nuserPassword: {USER_PASSWORD}"
        ),
        _group("staff", [person_dn("erika"), person_dn(SPECIAL_LOGIN)]),
        _group("admins", [group_dn("staff"), person_dn("max")]),
        _group("other", [person_dn("max")]),
        _group("cycle-a", [person_dn("jürgen"), group_dn("cycle-b")]),
        _group("cycle-b", [group_dn("cycle-a")]),
    ]
    return "\n\n".join(entries) + "\n"


@dataclass
class Slapd:
    ldap_port: int
    ldaps_port: int
    ca_pem: str
    process: subprocess.Popen[bytes]

    @property
    def ldap_url(self) -> str:
        return f"ldap://127.0.0.1:{self.ldap_port}"

    @property
    def ldaps_url(self) -> str:
        return f"ldaps://127.0.0.1:{self.ldaps_port}"

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


def start_slapd(workdir: Path, binaries: tuple[str, str]) -> Slapd:
    slapd, slapadd = binaries
    ca_key, ca_cert = make_ca()
    key, cert = _server_cert(ca_key, ca_cert)
    (workdir / "ca.pem").write_text(pem(ca_cert))
    (workdir / "server.pem").write_text(pem(cert))
    (workdir / "server.key").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (workdir / "db").mkdir()
    module_dir = next((d for d in MODULE_DIRS if (d / "back_mdb.so").exists()), None)
    modules = f"modulepath {module_dir}\nmoduleload back_mdb\n" if module_dir else ""
    schemas = "".join(
        f"include {SCHEMA_DIR / name}.schema\n"
        for name in ("core", "cosine", "inetorgperson", "nis", "msuser")
    )
    (workdir / "slapd.conf").write_text(
        f"""{schemas}{modules}
pidfile {workdir}/slapd.pid
TLSCACertificateFile {workdir}/ca.pem
TLSCertificateFile {workdir}/server.pem
TLSCertificateKeyFile {workdir}/server.key
database mdb
maxsize 33554432
suffix "{SUFFIX}"
rootdn "cn=admin,{SUFFIX}"
rootpw admin-secret-unused
directory {workdir}/db
access to attrs=userPassword by anonymous auth by * none
access to * by users read by * none
"""
    )
    (workdir / "data.ldif").write_text(test_data())
    subprocess.run(
        [slapadd, "-f", str(workdir / "slapd.conf"), "-l", str(workdir / "data.ldif")],
        check=True,
        capture_output=True,
    )
    ldap_port, ldaps_port = _free_port(), _free_port()
    urls = f"ldap://127.0.0.1:{ldap_port}/ ldaps://127.0.0.1:{ldaps_port}/"
    process = subprocess.Popen(
        [slapd, "-f", str(workdir / "slapd.conf"), "-h", urls, "-d", "0"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    server = Slapd(ldap_port, ldaps_port, pem(ca_cert), process)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"slapd exited with {process.returncode}")
        try:
            socket.create_connection(("127.0.0.1", ldaps_port), timeout=0.5).close()
            return server
        except OSError:
            time.sleep(0.1)
    server.stop()
    raise RuntimeError("slapd did not start")
