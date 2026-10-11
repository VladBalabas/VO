import os
from dataclasses import dataclass


def required_environment_variable(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задано обов'язкову змінну середовища {name}")
    return value


def positive_integer_environment_variable(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(f"Змінна середовища {name} має бути цілим числом") from error
    if value < 1:
        raise RuntimeError(f"Змінна середовища {name} має бути більшою за нуль")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    postgres_host: str
    postgres_port: int
    postgres_database: str
    postgres_user: str
    postgres_password: str
    database_pool_size: int


def load_settings() -> Settings:
    return Settings(
        postgres_host=required_environment_variable("POSTGRES_HOST"),
        postgres_port=positive_integer_environment_variable("POSTGRES_PORT", 5432),
        postgres_database=required_environment_variable("POSTGRES_DB"),
        postgres_user=required_environment_variable("POSTGRES_USER"),
        postgres_password=required_environment_variable("POSTGRES_PASSWORD"),
        database_pool_size=positive_integer_environment_variable("DB_POOL_SIZE", 15),
    )
