"""Запуск Telegram-бота-конструктора: python main.py"""
from manager import main
import asyncio

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nКонструктор остановлен.")
