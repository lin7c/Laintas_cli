import os
import sys

# When running as a PyInstaller frozen exe, Python's SSL cannot find the CA
# bundle via system paths. Point it at the certifi bundle we packed in.
if getattr(sys, 'frozen', False):
    try:
        import certifi
        ca_bundle = certifi.where()
        os.environ.setdefault('SSL_CERT_FILE', ca_bundle)
        os.environ.setdefault('REQUESTS_CA_BUNDLE', ca_bundle)
    except Exception:
        pass
