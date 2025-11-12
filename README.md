# Telegram Bot Copilot v2

*Multifunctional [Telegram bot](https://t.me/Terminatorvan_bot) for everyday tasks with multi-language support.*

## Main Features

- 🌊 Weather forecast
- 💵 Currency exchange rates
- 😺 Cat photos
- 📖 Cookbook
- 🍕 Pizza calculator
- 🙏 Donation system
- 🎮 Mini-games

## Technologies

- Backend: Python 3.11, aiogram 3.8.0
- Databases: PostgreSQL, Redis
- Monitoring: InfluxDB, Grafana
- Frontend (for mini-apps): JavaScript, HTML5, CSS3
- Containerization: Docker
- Localization: Babel/gettext

## Installation and Launch

1. Clone the repository
2. Create .env file based on .env.example and fill in the required environment variables
3. Launch the project via Docker Compose

## Project Structure
```
bot_copilot_v2/
├── app.py                 # Entry point
├── handlers/              # Command handlers
├── middlewares/           # Middleware processors
├── database/              # Database operations
├── common/                # Common components
├── locales/               # Localization files
└── docs/                  # Web applications
```


## Project Branches

- **main** - simplified version (PostgreSQL, 2 languages)
- **full-featured** - full version (PostgreSQL, Redis, InfluxDB, Grafana, 4 languages)


## Analytics

*Collecting and visualizing analytics using InfluxDB and Grafana*

![Analytics](common/images/image_anal.jpg)
