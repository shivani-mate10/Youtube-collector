"""Start Label Studio with a browser-default Referrer-Policy.

Label Studio sends `Referrer-Policy: same-origin`, so browsers send no Referer to YouTube and the
embedded player fails with "Error 153". This patches Django's security middleware to send
`strict-origin-when-cross-origin` instead, then runs Label Studio's normal command line.
Usage: python run_label_studio.py start --no-browser --port 8080
"""
import sys

from django.middleware import security

_original_init = security.SecurityMiddleware.__init__


def _init(self, get_response=None):
    _original_init(self, get_response)
    self.referrer_policy = "strict-origin-when-cross-origin"


security.SecurityMiddleware.__init__ = _init

from label_studio.server import main  # noqa: E402

if __name__ == "__main__":
    sys.argv[0] = "label-studio"
    sys.exit(main())
