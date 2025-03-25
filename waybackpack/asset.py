import logging
import re

from .session import Session
from .settings import DEFAULT_ROOT

logger = logging.getLogger(__name__)

ARCHIVE_TEMPLATE = "https://web.archive.org/web/{timestamp}{flag}/{url}"

REMOVAL_PATTERNS = [
    re.compile(
        b"<!-- BEGIN WAYBACK TOOLBAR INSERT -->.*?<!-- END WAYBACK TOOLBAR INSERT -->",
        re.DOTALL,
    ),
    re.compile(
        b'<script type="text/javascript" src="/static/js/analytics.js"></script>'
    ),
    re.compile(
        b'<script type="text/javascript">archive_analytics.values.server_name=[^<]+</script>'
    ),
    re.compile(
        b'<link type="text/css" rel="stylesheet" href="/static/css/banner-styles.css"/>'
    ),
]

REDIRECT_PATTERNS = [
    re.compile(rb"<p [^>]+>Got an HTTP (30\d) response at crawl time</p>"),
    re.compile(rb"<title>\s*Internet Archive Wayback Machine\s*</title>"),
    re.compile(rb'<a href="([^"]+)">Impatient\?</a>'),
]

# Patterns to find references to Wayback Machine URLs in HTML content
WEB_ARCHIVE_PATTERNS = [
    # Match /web/TIMESTAMP/URL pattern
    (re.compile(rb'(href|src)="(/web/\d+[a-z_]*/)(https?[^"]+)"', re.IGNORECASE), rb'\1="\3"'),
    # Match /web/TIMESTAMP/URL pattern for CSS url()
    (re.compile(rb'url\((/web/\d+[a-z_]*/)(https?[^)]+)\)', re.IGNORECASE), rb'url(\2)'),
    # Match /web/TIMESTAMP/im_/URL pattern for images
    (re.compile(rb'(href|src)="(/web/\d+im_/)(https?[^"]+)"', re.IGNORECASE), rb'\1="\3"'),
    # Match /web/TIMESTAMP/js_/URL pattern for javascript
    (re.compile(rb'(href|src)="(/web/\d+js_/)(https?[^"]+)"', re.IGNORECASE), rb'\1="\3"'),
    # Match /web/TIMESTAMP/cs_/URL pattern for CSS
    (re.compile(rb'(href|src)="(/web/\d+cs_/)(https?[^"]+)"', re.IGNORECASE), rb'\1="\3"'),
    # Match https://web-static.archive.org/_static/ URLs
    (re.compile(rb'(href|src)="(https?://web-static\.archive\.org/_static/[^"]+)"', re.IGNORECASE), rb''),
    # Match full Wayback URL https://web.archive.org/web/TIMESTAMP/URL
    (re.compile(rb'(href|src)="(https?://web\.archive\.org/web/\d+[a-z_]*/)([^"]+)"', re.IGNORECASE), rb'\1="\3"'),
    # Remove references to archive.org JavaScript files
    (re.compile(rb'<script[^>]*src="(https?://web(?:-static)?\.archive\.org/[^"]+)"[^>]*>.*?</script>', re.DOTALL | re.IGNORECASE), rb''),
    # Remove references to archive.org CSS files
    (re.compile(rb'<link[^>]*href="(https?://web(?:-static)?\.archive\.org/[^"]+)"[^>]*/?>', re.IGNORECASE), rb''),
    # Fix integrity attributes that might cause issues 
    (re.compile(rb'integrity="[^"]+"', re.IGNORECASE), rb''),
    # Fix crossorigin attributes that might cause issues
    (re.compile(rb'crossorigin="[^"]+"', re.IGNORECASE), rb''),
]

# CSS specific patterns for rewriting URLs in stylesheets
CSS_URL_PATTERNS = [
    # Match url('/web/TIMESTAMP/URL') pattern
    (re.compile(rb'url\([\'"]?(/web/\d+[a-z_]*/)([^\'")]+)[\'"]?\)', re.IGNORECASE), rb'url("\2")'),
    # Match url('https://web.archive.org/web/TIMESTAMP/URL') pattern
    (re.compile(rb'url\([\'"]?(https?://web\.archive\.org/web/\d+[a-z_]*/)([^\'")]+)[\'"]?\)', re.IGNORECASE), rb'url("\2")'),
    # Match @import '/web/TIMESTAMP/URL' pattern
    (re.compile(rb'@import\s+[\'"]?(/web/\d+[a-z_]*/)([^\'";]+)[\'"]?', re.IGNORECASE), rb'@import "\2"'),
    # Match @import 'https://web.archive.org/web/TIMESTAMP/URL' pattern
    (re.compile(rb'@import\s+[\'"]?(https?://web\.archive\.org/web/\d+[a-z_]*/)([^\'";]+)[\'"]?', re.IGNORECASE), rb'@import "\2"'),
    # Remove web-static.archive.org references
    (re.compile(rb'url\([\'"]?(https?://web-static\.archive\.org/_static/[^\'"]+)[\'"]?\)', re.IGNORECASE), rb''),
]

# Additional cleanup for HTML that needs to be performed
ADDITIONAL_CLEANUP = [
    # Remove the Wayback Machine banner insertion
    (re.compile(rb'<!-- BEGIN WAYBACK TOOLBAR INSERT -->.*?<!-- END WAYBACK TOOLBAR INSERT -->', re.DOTALL), b''),
    # Remove the Wayback Machine toolbar HTML
    (re.compile(rb'<div id="wm-ipp-base".*?</div>', re.DOTALL), b''),
    # Remove any __wm references
    (re.compile(rb'<script[^>]*>\s*__wm\.init\([^<]*</script>', re.DOTALL), b''),
    # Remove the Wayback analytics
    (re.compile(rb'<script[^>]*>\s*window\.analytics[^<]*</script>', re.DOTALL), b''),
]

class Asset(object):
    def __init__(self, original_url, timestamp):
        # Ensure timestamp is only numeric
        if re.match(r"^[0-9]+\Z", timestamp) is None:
            raise RuntimeError("invalid timestamp {!r}".format(timestamp))
        self.timestamp = timestamp
        self.original_url = original_url

    def get_archive_url(self, raw=False):
        """
        Generate the Wayback Machine URL for this asset
        """
        flag = "id_" if raw else ""
        return ARCHIVE_TEMPLATE.format(
            timestamp=self.timestamp,
            url=self.original_url,
            flag=flag,
        )

    def fetch(self, session=None, raw=False, root=DEFAULT_ROOT, rate_limiter=None):
        """
        Fetch the asset from the Wayback Machine
        
        Parameters:
        - session: Session object for HTTP requests
        - raw: If True, fetch the raw, unprocessed asset
        - root: Root URL for resolving relative paths
        - rate_limiter: Optional RateLimiter object to control request rate
        
        Returns the content of the asset
        """
        session = session or Session()
        url = self.get_archive_url(raw)
        
        # Apply rate limiting if provided
        if rate_limiter is not None:
            rate_limiter.wait_if_needed()
            
        res = session.get(url)

        if res is None:
            return None

        content = res.content
        if raw:
            return content

        else:
            rdp = REDIRECT_PATTERNS

            is_js_redirect = sum(
                re.search(pat, content) is not None for pat in rdp
            ) == len(rdp)

            if is_js_redirect:
                code = re.search(rdp[0], content).group(1).decode("utf-8")
                loc = DEFAULT_ROOT + re.search(rdp[2], content).group(1).decode("utf-8")
                log_msg = "Encountered {0} redirect to {1}."
                logger.info(log_msg.format(code, loc))
                if session.follow_redirects:
                    # Apply rate limiting for redirect request
                    if rate_limiter is not None:
                        rate_limiter.wait_if_needed()
                    content = session.get(loc).content
                else:
                    pass

            # Get the content type to determine what kind of processing to do
            content_type = res.headers.get('Content-Type', '').lower()
            
            # Remove Wayback Machine toolbar and other elements
            if re.search(REMOVAL_PATTERNS[0], content) is not None or is_html_content(content):
                for pat in REMOVAL_PATTERNS:
                    content = re.sub(pat, b"", content)
                
                # Process HTML content
                if content_type.startswith('text/html') or 'html' in content_type or is_html_content(content):
                    content = self._process_html_content(content)
                # Process CSS content
                elif content_type.startswith('text/css') or self.original_url.endswith('.css'):
                    content = self._process_css_content(content)
                
                # Fix root references in the content 
                if root != "":
                    root_pat = re.compile(
                        ("(['\"])(/web/" + self.timestamp + ")").encode("utf-8")
                    )
                    content = re.sub(
                        root_pat, (r"\1" + root + r"\2").encode("utf-8"), content
                    )
            
            return content
            
    def _process_html_content(self, content):
        """
        Process HTML content to replace Wayback Machine references
        """
        # Replace references to the Wayback Machine
        # This is needed to fix links to resources like CSS, JS, images, etc.
        for pattern, replacement in WEB_ARCHIVE_PATTERNS:
            if replacement:
                content = re.sub(pattern, replacement, content)
            else:
                # For patterns that should be completely removed
                content = re.sub(pattern, b'', content)
        
        # Run additional cleanup patterns
        for pattern, replacement in ADDITIONAL_CLEANUP:
            content = re.sub(pattern, replacement, content)
        
        # Handle additional cleanup for script tags that might be injected
        content = re.sub(
            rb'<script[^>]*wombat\.js[^>]*>.*?</script>',
            b'',
            content,
            flags=re.DOTALL
        )
        
        # Remove Ruffle player scripts (for Flash)
        content = re.sub(
            rb'<script[^>]*ruffle\.js[^>]*>.*?</script>',
            b'',
            content,
            flags=re.DOTALL
        )
        
        # Remove __wm init scripts
        content = re.sub(
            rb'<script[^>]*>\s*__wm\.init\([^<]*</script>',
            b'',
            content,
            flags=re.DOTALL
        )
        
        # Remove window.RufflePlayer scripts
        content = re.sub(
            rb'<script>window\.RufflePlayer[^<]*</script>',
            b'',
            content,
            flags=re.DOTALL
        )
        
        return content
        
    def _process_css_content(self, content):
        """
        Process CSS content to replace Wayback Machine references
        """
        # Apply CSS-specific patterns to rewrite URLs in stylesheets
        for pattern, replacement in CSS_URL_PATTERNS:
            content = re.sub(pattern, replacement, content)
        
        return content


def is_html_content(content):
    """Helper function to determine if content is HTML"""
    if not content:
        return False
    
    sample = content[:1000].lower()  # Check just the beginning
    return b'<!doctype html>' in sample or b'<html' in sample
