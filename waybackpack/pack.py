import logging
import os
import platform
import time
import re
from bs4 import BeautifulSoup
import urllib.parse
from collections import deque
from threading import Lock

from .asset import Asset
from .cdx import search
from .session import Session
from .settings import DEFAULT_ROOT

logger = logging.getLogger(__name__)

try:
    from urllib.parse import urlparse, urljoin
except ImportError:
    from urlparse import urlparse, urljoin

try:
    from tqdm.auto import tqdm

    has_tqdm = True
except ImportError:
    has_tqdm = False

psl = platform.system().lower()
if "windows" in psl or "cygwin" in psl:
    invalid_chars = '<>:"\\|?*'
elif "darwin" in psl:
    invalid_chars = ":"
else:
    invalid_chars = ""


def replace_invalid_chars(path, fallback_char="_"):
    path = "".join([fallback_char if c in invalid_chars else c for c in path])
    return os.path.join(
        *(
            fallback_char * len(part) if part in {os.curdir, os.pardir} else part
            for part in path.split("/")
        )
    )


class RateLimiter:
    """
    Rate limiter to ensure no more than max_requests are made within the time window
    """
    def __init__(self, max_requests=14, window_seconds=60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.request_timestamps = deque()
        self.lock = Lock()
        
    def wait_if_needed(self):
        """Wait if the rate limit would be exceeded"""
        with self.lock:
            now = time.time()
            
            # Remove timestamps older than our window
            while self.request_timestamps and now - self.request_timestamps[0] > self.window_seconds:
                self.request_timestamps.popleft()
            
            # If we've hit our limit, calculate wait time
            if len(self.request_timestamps) >= self.max_requests:
                # Wait until the oldest request falls out of our window
                wait_time = self.window_seconds - (now - self.request_timestamps[0])
                if wait_time > 0:
                    logger.info(f"Rate limit reached. Waiting {wait_time:.2f} seconds...")
                    time.sleep(wait_time)
                    # Update now after sleeping
                    now = time.time()
            
            # Add the current timestamp
            self.request_timestamps.append(now)


class Pack(object):
    def __init__(self, url, timestamps=None, uniques_only=False, session=None, rate_limit=None):

        self.url = url
        prefix = "http://" if urlparse(url).scheme == "" else ""
        self.full_url = prefix + url
        self.parsed_url = urlparse(self.full_url)

        self.session = session or Session()
        
        # Rate limiter - default to 14 requests per minute
        self.rate_limiter = rate_limit or RateLimiter(max_requests=14, window_seconds=60)

        if timestamps is None:
            self.timestamps = [
                snap["timestamp"]
                for snap in search(url, uniques_only=uniques_only, session=self.session)
            ]
        else:
            self.timestamps = timestamps

        self.assets = [Asset(self.url, ts) for ts in self.timestamps]
        
    def _extract_resources(self, html_content, timestamp, base_url):
        """
        Extract CSS, JS, and other assets from HTML content
        Returns a list of URLs for resources to download
        """
        resources = []
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Extract CSS files
            for link in soup.find_all('link', rel='stylesheet'):
                if 'href' in link.attrs:
                    resources.append(link['href'])
            
            # Extract JS files
            for script in soup.find_all('script', src=True):
                resources.append(script['src'])
            
            # Extract images
            for img in soup.find_all('img', src=True):
                resources.append(img['src'])

            # Extract CSS @import and url() references
            style_tags = soup.find_all('style')
            for style in style_tags:
                if style.string:
                    # Look for @import url() in style tags
                    import_urls = re.findall(r'@import\s+url\(["\']?([^"\'()]+)["\']?\)', style.string)
                    for url in import_urls:
                        if url and not url.startswith('data:'):
                            resources.append(url)
                    
                    # Look for url() in style tags
                    css_urls = re.findall(r'url\(["\']?([^"\'()]+)["\']?\)', style.string)
                    for url in css_urls:
                        if url and not url.startswith('data:'):
                            resources.append(url)
                
            # Extract other media (video, audio)
            for source in soup.find_all('source', src=True):
                resources.append(source['src'])
                
            # Extract favicons
            for link in soup.find_all('link', rel=lambda x: x and ('icon' in x.lower())):
                if 'href' in link.attrs:
                    resources.append(link['href'])
                    
            # Extract background images in inline styles
            elements_with_style = soup.find_all(style=True)
            for element in elements_with_style:
                style_content = element['style']
                bg_urls = re.findall(r'url\(["\']?([^"\'()]+)["\']?\)', style_content)
                for url in bg_urls:
                    if url and not url.startswith('data:'):
                        resources.append(url)
                        
            # Extract iframes
            for iframe in soup.find_all('iframe', src=True):
                resources.append(iframe['src'])
            
            # Process the URLs to make them absolute
            processed_resources = []
            seen_urls = set()  # To avoid duplicates
            
            for resource_url in resources:
                # Skip data URLs, anchors, and javascript:
                if resource_url.startswith(('data:', '#', 'javascript:', 'about:')):
                    continue
                    
                # Remove any wayback machine prefixes that might still be in the URLs
                resource_url = re.sub(r'^(?:https?://web\.archive\.org)?/web/\d+[a-z_]*/', '', resource_url)
                
                # Make relative URLs absolute
                if not resource_url.startswith(('http://', 'https://', '//')):
                    resource_url = urljoin(base_url, resource_url)
                
                # Normalize the URL
                if resource_url.startswith('//'):
                    resource_url = 'https:' + resource_url
                
                # Only add unique URLs
                normalized_url = resource_url.rstrip('/')
                if normalized_url not in seen_urls:
                    seen_urls.add(normalized_url)
                    processed_resources.append(normalized_url)
                
            return processed_resources
        except Exception as e:
            logger.warn(f"Error extracting resources: {e}")
            return []
            
    def _download_resource(self, resource_url, timestamp, directory, raw, root, 
                          ignore_errors, no_clobber, delay, fallback_char):
        """
        Download a resource (CSS, JS, image, etc.) from the Wayback Machine
        """
        try:
            # If the URL contains wayback machine references, clean them up
            resource_url = re.sub(r'^(?:https?://web\.archive\.org)?/web/\d+[a-z_]*/', '', resource_url)
            
            # Create an Asset object for the resource
            asset = Asset(resource_url, timestamp)
            
            parsed_resource = urlparse(resource_url)
            path_head, path_tail = os.path.split(parsed_resource.path)
            
            # Use the last part of the path as the filename
            if path_tail == "":
                # If there's no filename, use the last directory part with .html extension
                # for paths that might represent directory indexes
                path_parts = parsed_resource.path.rstrip('/').split('/')
                if path_parts:
                    path_tail = path_parts[-1] + ".html"
                else:
                    path_tail = "index.html"
                if not path_tail:
                    logger.info(f"Skipping resource with no filename: {resource_url}")
                    return None
                
            # Add appropriate extension for JS/CSS if needed
            if not path_tail.endswith(('.js', '.css', '.html', '.htm', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico')):
                content_type = self._guess_content_type(resource_url)
                if 'javascript' in content_type:
                    path_tail += '.js'
                elif 'css' in content_type:
                    path_tail += '.css'
                elif 'html' in content_type:
                    path_tail += '.html'
                elif 'image' in content_type:
                    if 'png' in content_type:
                        path_tail += '.png'
                    elif 'jpeg' in content_type or 'jpg' in content_type:
                        path_tail += '.jpg'
                    elif 'gif' in content_type:
                        path_tail += '.gif'
                    elif 'svg' in content_type:
                        path_tail += '.svg'
                    else:
                        path_tail += '.img'
                        
            # Construct the local file path for the resource
            filedir = os.path.join(
                directory,
                timestamp,
                replace_invalid_chars(parsed_resource.netloc, fallback_char),
                replace_invalid_chars(path_head.lstrip("/"), fallback_char),
            )
            
            filepath = os.path.join(
                filedir, replace_invalid_chars(path_tail, fallback_char)
            )
            
            if no_clobber and (
                os.path.exists(filepath) and os.path.getsize(filepath) > 0
            ):
                logger.info(f"Skipping existing file: {filepath}")
                return None
                
            logger.info(
                f"Fetching resource {asset.original_url} @ {asset.timestamp}"
            )
            
            # Apply rate limiting before making the request
            self.rate_limiter.wait_if_needed()
            
            # Fetch the resource content
            try:
                content = asset.fetch(session=self.session, raw=raw, root=root, rate_limiter=self.rate_limiter)
                if content is None:
                    return None
            except Exception as e:
                if ignore_errors:
                    ex_name = ".".join([e.__module__, e.__class__.__name__])
                    logger.warn(
                        f"ERROR -- {asset.original_url} @ {asset.timestamp} -- {ex_name}: {e}"
                    )
                    return None
                else:
                    raise
                    
            # Create the directory and save the file
            try:
                os.makedirs(filedir, exist_ok=True)
            except OSError:
                pass
                
            with open(filepath, "wb") as f:
                logger.info(f"Writing resource to {filepath}")
                f.write(content)
                
            return filepath
        except Exception as e:
            if ignore_errors:
                logger.warn(f"Error downloading resource {resource_url}: {e}")
                return None
            else:
                raise

    def _guess_content_type(self, url):
        """
        Guess the content type from the URL if possible
        """
        lower_url = url.lower()
        if '.js' in lower_url or 'javascript' in lower_url:
            return 'javascript'
        elif '.css' in lower_url or 'stylesheet' in lower_url:
            return 'css'
        elif any(ext in lower_url for ext in ['.html', '.htm']):
            return 'html'
        elif any(ext in lower_url for ext in ['.jpg', '.jpeg']):
            return 'image/jpeg'
        elif '.png' in lower_url:
            return 'image/png'
        elif '.gif' in lower_url:
            return 'image/gif'
        elif '.svg' in lower_url:
            return 'image/svg'
        else:
            return 'unknown'

    def download_to(
        self,
        directory,
        raw=False,
        root=DEFAULT_ROOT,
        ignore_errors=False,
        no_clobber=False,
        progress=False,
        delay=0,
        fallback_char="_",
        download_assets=True,
    ):

        if progress and not has_tqdm:
            raise Exception(
                "To print progress bars, you must have `tqdm` installed. To install: pip install tqdm."
            )

        for i, asset in enumerate(tqdm(self.assets) if progress else self.assets):
            path_head, path_tail = os.path.split(self.parsed_url.path)
            if path_tail == "":
                path_tail = "index.html"

            filedir = os.path.join(
                directory,
                asset.timestamp,
                replace_invalid_chars(self.parsed_url.netloc, fallback_char),
                replace_invalid_chars(path_head.lstrip("/"), fallback_char),
            )

            filepath = os.path.join(
                filedir, replace_invalid_chars(path_tail, fallback_char)
            )

            if no_clobber and (
                os.path.exists(filepath) and os.path.getsize(filepath) > 0
            ):
                continue

            # Use our own delay or the rate limiter
            if i > 0 and delay:
                logger.info("Sleeping {0} seconds".format(delay))
                time.sleep(delay)
            else:
                # Apply rate limiting
                self.rate_limiter.wait_if_needed()

            logger.info(
                "Fetching {0} @ {1}".format(asset.original_url, asset.timestamp)
            )

            try:
                content = asset.fetch(session=self.session, raw=raw, root=root, rate_limiter=self.rate_limiter)

                if content is None:
                    continue

            except Exception as e:
                if ignore_errors is True:
                    ex_name = ".".join([e.__module__, e.__class__.__name__])
                    logger.warn(
                        "ERROR -- {0} @ {1} -- {2}: {3}".format(
                            asset.original_url, asset.timestamp, ex_name, e
                        )
                    )
                    continue
                else:
                    raise

            try:
                os.makedirs(filedir, exist_ok=True)
            except OSError:
                pass

            with open(filepath, "wb") as f:
                logger.info("Writing to {0}\n".format(filepath))
                f.write(content)
                
            # If download_assets is enabled, process the file appropriately based on its type
            if download_assets and not raw:
                # Check content type to determine how to handle it
                content_type = self._guess_content_type_from_path(filepath)
                
                # For HTML files, extract resources using BeautifulSoup
                if path_tail.endswith(('.html', '.htm')) or 'text/html' in str(content[:1000]):
                    try:
                        # Extract and download linked resources
                        resources = self._extract_resources(content, asset.timestamp, asset.original_url)
                        logger.info(f"Found {len(resources)} resources to download")
                        
                        # Download each resource
                        for resource_url in resources:
                            # Skip delay here as we'll use rate limiting
                            self._download_resource(
                                resource_url, 
                                asset.timestamp,
                                directory,
                                raw,
                                root,
                                ignore_errors,
                                no_clobber,
                                delay,
                                fallback_char
                            )
                    except Exception as e:
                        if ignore_errors:
                            logger.warn(f"Error processing HTML assets: {e}")
                        else:
                            raise
                
                # For CSS files, extract and download any url() references
                elif path_tail.endswith('.css') or content_type == 'text/css':
                    try:
                        # Extract URL references from CSS
                        css_resources = self._extract_css_resources(content, asset.timestamp, asset.original_url)
                        if css_resources:
                            logger.info(f"Found {len(css_resources)} resources in CSS file")
                            
                            # Download each CSS resource
                            for resource_url in css_resources:
                                self._download_resource(
                                    resource_url,
                                    asset.timestamp,
                                    directory,
                                    raw,
                                    root,
                                    ignore_errors,
                                    no_clobber,
                                    delay,
                                    fallback_char
                                )
                    except Exception as e:
                        if ignore_errors:
                            logger.warn(f"Error processing CSS assets: {e}")
                        else:
                            raise
    
    def _extract_css_resources(self, content, timestamp, base_url):
        """
        Extract resources referenced in CSS files (url() and @import)
        """
        resources = []
        try:
            # Decode content if it's bytes
            if isinstance(content, bytes):
                # Try different encodings
                for encoding in ['utf-8', 'latin-1', 'ascii']:
                    try:
                        content_str = content.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    # If all decodings fail, use latin-1 which should at least not error
                    content_str = content.decode('latin-1', errors='replace')
            else:
                content_str = content
                
            # Extract url() references
            url_matches = re.findall(r'url\([\'"]?([^\'")]+)[\'"]?\)', content_str)
            for url in url_matches:
                if url and not url.startswith('data:'):
                    # Remove any wayback machine prefixes
                    url = re.sub(r'^(?:https?://web\.archive\.org)?/web/\d+[a-z_]*/', '', url)
                    resources.append(url)
                
            # Extract @import references
            import_matches = re.findall(r'@import\s+[\'"]([^\'";]+)[\'"]', content_str)
            for url in import_matches:
                if url and not url.startswith('data:'):
                    # Remove any wayback machine prefixes
                    url = re.sub(r'^(?:https?://web\.archive\.org)?/web/\d+[a-z_]*/', '', url)
                    resources.append(url)
                    
            # Process the URLs to make them absolute
            processed_resources = []
            seen_urls = set()  # To avoid duplicates
            
            for resource_url in resources:
                # Skip data URLs, anchors, and javascript:
                if resource_url.startswith(('data:', '#', 'javascript:', 'about:')):
                    continue
                
                # Make relative URLs absolute
                if not resource_url.startswith(('http://', 'https://', '//')):
                    # For CSS files, paths may be relative to the CSS file location
                    # Get the base directory of the CSS file
                    css_dir = os.path.dirname(urlparse(base_url).path)
                    if resource_url.startswith('/'):
                        # Absolute path relative to domain
                        domain = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
                        resource_url = f"{domain}{resource_url}"
                    else:
                        # Relative path to the CSS file
                        domain = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"
                        if css_dir and css_dir != '/':
                            full_path = f"{css_dir}/{resource_url}"
                        else:
                            full_path = f"/{resource_url}"
                        resource_url = f"{domain}{full_path}"
                
                # Normalize the URL
                if resource_url.startswith('//'):
                    resource_url = 'https:' + resource_url
                
                # Only add unique URLs
                normalized_url = resource_url.rstrip('/')
                if normalized_url not in seen_urls:
                    seen_urls.add(normalized_url)
                    processed_resources.append(normalized_url)
                    
            return processed_resources
        except Exception as e:
            logger.warn(f"Error extracting CSS resources: {e}")
            return []
            
    def _guess_content_type_from_path(self, filepath):
        """
        Guess the content type from the file path
        """
        filepath = filepath.lower()
        if filepath.endswith('.css'):
            return 'text/css'
        elif filepath.endswith('.js'):
            return 'text/javascript'
        elif filepath.endswith(('.html', '.htm')):
            return 'text/html'
        elif filepath.endswith(('.jpg', '.jpeg')):
            return 'image/jpeg'
        elif filepath.endswith('.png'):
            return 'image/png'
        elif filepath.endswith('.gif'):
            return 'image/gif'
        elif filepath.endswith('.svg'):
            return 'image/svg+xml'
        else:
            return 'application/octet-stream'
