from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Claude API
    anthropic_api_key: str = Field(..., alias="ANTHROPIC_API_KEY")

    # massive.com
    massive_api_key: str = Field(..., alias="MASSIVE_API_KEY")
    massive_base_url: str = Field("https://api.massive.com", alias="MASSIVE_BASE_URL")

    # Database
    database_url: str = Field(..., alias="DATABASE_URL")

    # Email
    email_recipient: str = Field(..., alias="EMAIL_RECIPIENT")
    email_from: str = Field(..., alias="EMAIL_FROM")
    smtp_host: str = Field("smtp.gmail.com", alias="SMTP_HOST")
    smtp_port: int = Field(587, alias="SMTP_PORT")
    smtp_user: str = Field(..., alias="SMTP_USER")
    smtp_pass: str = Field(..., alias="SMTP_PASS")

    # Newsletter
    newsletter_timezone: str = Field("America/New_York", alias="NEWSLETTER_TIMEZONE")
    newsletter_send_time: str = Field("07:20", alias="NEWSLETTER_SEND_TIME")
    pipeline_start_time: str = Field("05:00", alias="PIPELINE_START_TIME")

    # Portfolio
    portfolio_csv_path: str = Field("portfolio.csv", alias="PORTFOLIO_CSV_PATH")
    portfolios_config: str = Field("portfolios.json", alias="PORTFOLIOS_CONFIG")

    # GitHub
    github_token: str = Field("", alias="GITHUB_TOKEN")
    github_repo: str = Field("your-username/financial-bytes", alias="GITHUB_REPO")

    # CNBC / Queryly
    queryly_api_key: str = Field("31a35d40a9a64ab3", alias="QUERYLY_API_KEY")

    # Claude agent permissions
    claude_skip_permissions: bool = Field(True, alias="CLAUDE_SKIP_PERMISSIONS")

    # Scraper
    scraper_delay_min: float = Field(2.0, alias="SCRAPER_DELAY_MIN")
    scraper_delay_max: float = Field(5.0, alias="SCRAPER_DELAY_MAX")
    max_articles_per_ticker: int = Field(15, alias="MAX_ARTICLES_PER_TICKER")
    article_lookback_hours: int = Field(24, alias="ARTICLE_LOOKBACK_HOURS")

    # Parallelism
    max_parallel_tickers: int = Field(3, alias="MAX_PARALLEL_TICKERS")
    max_parallel_analysts: int = Field(5, alias="MAX_PARALLEL_ANALYSTS")

    # Logging
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_file: str = Field("logs/financial_bytes.log", alias="LOG_FILE")


settings = Settings()
