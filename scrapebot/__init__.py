"""scrapebot — an orchestrated fleet of stealth Playwright scraping bots.

Each worker bot drives a Chromium session whose fingerprint comes from a
configured browser profile, whose traffic exits through a Gluetun VPN
container, and whose pace is bounded by fleet-wide scrape limits.
"""

__version__ = "0.1.0"
