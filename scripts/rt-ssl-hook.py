# SPDX-License-Identifier: AGPL-3.0-only
"""PyInstaller runtime hook for the standalone rt (scripts/build-rt.sh).

The executable carries the OpenSSL of the system it was built on, which looks for trusted
certificates where that system keeps them. Point it at this computer's own bundle instead, unless
SSL_CERT_FILE or SSL_CERT_DIR already names one.
"""
import os

if not os.environ.get("SSL_CERT_FILE") and not os.environ.get("SSL_CERT_DIR"):
    for bundle in ("/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu, Arch, Alpine
                   "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
                   "/etc/ssl/ca-bundle.pem",  # openSUSE
                   "/etc/ssl/cert.pem"):  # macOS
        if os.path.isfile(bundle):
            os.environ["SSL_CERT_FILE"] = bundle
            break
