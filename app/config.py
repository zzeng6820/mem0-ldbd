from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    wrapper_api_key: str = ""
    mem0_api_key: str = ""
    mem0_base_url: str = "https://api.mem0.ai"
    mem0_api_mode: str = "legacy"
    db_path: str = "data/requests.sqlite3"
    upstream_timeout_seconds: float = 60
    poll_interval_seconds: float = 1
    add_wait_seconds: float = 600
    max_add_concurrency: int = 64
    max_search_concurrency: int = 64

    def __post_init__(self):
        if self.mem0_api_mode not in {"legacy", "v3"}:
            raise ValueError("MEM0_API_MODE must be legacy or v3")
        if not self.mem0_base_url.startswith("https://"):
            raise ValueError("MEM0_BASE_URL must use HTTPS")
        if min(self.upstream_timeout_seconds, self.poll_interval_seconds,
               self.add_wait_seconds, self.max_add_concurrency,
               self.max_search_concurrency) <= 0:
            raise ValueError("Timeouts, polling interval and concurrency must be positive")

    @classmethod
    def from_env(cls):
        return cls(
            wrapper_api_key=os.getenv("WRAPPER_API_KEY", ""),
            mem0_api_key=os.getenv("MEM0_API_KEY", ""),
            mem0_base_url=os.getenv("MEM0_BASE_URL", "https://api.mem0.ai").rstrip("/"),
            mem0_api_mode=os.getenv("MEM0_API_MODE", "legacy"),
            db_path=os.getenv("REQUEST_DB_PATH", "data/requests.sqlite3"),
            upstream_timeout_seconds=float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", "60")),
            poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "1")),
            add_wait_seconds=float(os.getenv("ADD_WAIT_SECONDS", "600")),
            max_add_concurrency=int(os.getenv("MAX_ADD_CONCURRENCY", "64")),
            max_search_concurrency=int(os.getenv("MAX_SEARCH_CONCURRENCY", "64")),
        )
