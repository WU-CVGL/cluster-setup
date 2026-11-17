import secrets


class ContentSecurityPolicy:
    """Only this origin, and the page's own inline style and script, which carry a nonce per response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.csp_nonce = secrets.token_urlsafe(18)
        response = self.get_response(request)
        nonce = request.csp_nonce
        response.setdefault("Content-Security-Policy", (
            "default-src 'none'; "
            f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
            "img-src 'self' data:; connect-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'"))
        response.setdefault("Cache-Control", "no-store")
        return response
