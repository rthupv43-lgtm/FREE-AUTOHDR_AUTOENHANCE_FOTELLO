"""TLS defaults shared by source runs and packaged desktop builds."""

import os
import ssl

import certifi


def configure_ca_bundle() -> str:
    ca_bundle = certifi.where()
    if not os.path.isfile(ca_bundle):
        raise RuntimeError(
            f"Không tìm thấy CA certificate bundle: {ca_bundle}"
        )

    os.environ["SSL_CERT_FILE"] = ca_bundle
    return ca_bundle


def create_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=configure_ca_bundle())
