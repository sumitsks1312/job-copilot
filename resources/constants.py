# ---------------------------------------------------------------------------
# Application-wide constants — edit here to tune behaviour without touching
# service code.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Job Search
# ---------------------------------------------------------------------------

# Number of JSearch result pages per query (10 jobs per page).
# 1 → 10 results, 2 → 20, 3 → 30, etc.
JSEARCH_NUM_PAGES: int = 1

# HTTP timeout in seconds for each JSearch API request.
JSEARCH_TIMEOUT_SECONDS: int = 12

# ---------------------------------------------------------------------------
# Job Search Cache
# ---------------------------------------------------------------------------

# How long (in seconds) a cached result is considered fresh.
# Default: 24 hours.
CACHE_TTL_SECONDS: int = 24 * 60 * 60
