import logging

from .session import Session

logger = logging.getLogger(__name__)

SEARCH_URL = "https://web.archive.org/cdx/search/cdx"


class WaybackpackException(Exception):
    pass


def search(
    url, from_date=None, to_date=None, uniques_only=False, collapse=None, session=None, rate_limiter=None
):
    """
    Search the Wayback Machine CDX API for snapshots of a URL.
    
    Parameters:
    - url: The URL to search for
    - from_date: Optional start date
    - to_date: Optional end date
    - uniques_only: If True, return only unique captures
    - collapse: Optional collapse parameter
    - session: Session object for HTTP requests
    - rate_limiter: Optional RateLimiter object to control request rate
    
    Returns a list of snapshots matching the criteria.
    """
    session = session or Session()
    
    # Apply rate limiting if a limiter is provided
    if rate_limiter is not None:
        rate_limiter.wait_if_needed()
        
    res = session.get(
        SEARCH_URL,
        params={
            "url": url,
            "from": from_date,
            "to": to_date,
            "showDupeCount": "true",
            "output": "json",
            "collapse": collapse,
        },
    )
    if res is None:
        raise WaybackpackException("Difficulty connecting to Wayback Machine CDX API")

    if res.status_code == 200:
        cdx = res.json()

    else:
        log_msg = 'CDX exception: "{0}"'
        logger.info(log_msg.format(res.content.decode("utf-8").strip()))
        return []

    if len(cdx) < 2:
        return []
    fields = cdx[0]
    snapshots = [dict(zip(fields, row)) for row in cdx[1:]]
    if uniques_only:
        if len(snapshots) and "dupecount" not in snapshots[0]:
            raise WaybackpackException(
                "Wayback Machine CDX API not respecting showDupeCount=true; retry without --uniques-only."
            )
        return [s for s in snapshots if int(s["dupecount"]) == 0]
    else:
        return snapshots
