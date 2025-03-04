from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
import asyncio
import os
from models import db, User, Channel, BotConfig
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
from flask import Flask

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

# Database configuration
db_url = os.environ.get('DATABASE_URL')
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

print("Initializing bot with database connection...")

async def forward_messages():
    try:
        print("\n--- Starting message forwarding loop ---")
        # Get all active bot configurations
        bot_configs = BotConfig.query.filter_by(is_active=True).all()
        print(f"Found {len(bot_configs)} active bot configurations")

        for config in bot_configs:
            print(f"\nProcessing configuration for user {config.user_id}")

            # Get user's channels
            source_channels = Channel.query.filter_by(
                user_id=config.user_id,
                is_source=True
            ).all()
            print(f"Found {len(source_channels)} source channels")

            destination_channels = Channel.query.filter_by(
                user_id=config.user_id,
                is_destination=True
            ).all()
            print(f"Found {len(destination_channels)} destination channels")

            if not source_channels or not destination_channels:
                print(f"No channel configuration found for user {config.user_id}")
                continue

            # Get user's phone number for session
            user = User.query.get(config.user_id)
            if not user or not user.phone:
                print(f"No valid user found for ID {config.user_id}")
                continue

            try:
                print(f"\nSetting up client for user {user.phone}")
                client = TelegramClient(f"sessions/{user.phone}", API_ID, API_HASH)
                await client.connect()

                if not await client.is_user_authorized():
                    print(f"Client not authorized for user {user.phone}")
                    continue

                print(f"Client authorized for user {user.phone}")

                # Set up message handler for each source channel
                for source in source_channels:
                    print(f"\nSetting up handler for source channel {source.telegram_channel_id}")

                    @client.on(events.NewMessage(chats=int(source.telegram_channel_id)))
                    async def forward_handler(event):
                        try:
                            print(f"\nNew message received in source channel {source.telegram_channel_id}")

                            for dest in destination_channels:
                                try:
                                    print(f"Forwarding to destination channel {dest.telegram_channel_id}")
                                    # Forward the message
                                    forwarded = await client.forward_messages(
                                        int(dest.telegram_channel_id),
                                        event.message
                                    )
                                    if forwarded:
                                        print(f"✓ Message successfully forwarded to {dest.telegram_channel_id}")
                                    else:
                                        print(f"! Message forwarding failed to {dest.telegram_channel_id}")
                                except Exception as e:
                                    print(f"Error forwarding to {dest.telegram_channel_id}: {str(e)}")

                        except Exception as e:
                            print(f"Error in forward handler: {str(e)}")

                print(f"Message forwarding set up for user {user.phone}")

            except Exception as e:
                print(f"Error setting up client for user {user.phone}: {str(e)}")
                continue

            # Keep the client running
            try:
                print(f"\nStarting client for user {user.phone}")
                await client.run_until_disconnected()
            except Exception as e:
                print(f"Client disconnected for user {user.phone}: {str(e)}")

    except Exception as e:
        print(f"Error in forward_messages: {str(e)}")

async def main():
    print("Starting Telegram bot service...")

    # Initialize database connection
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)

    with app.app_context():
        db.create_all()  # Ensure tables exist

        while True:
            try:
                await forward_messages()
                print("\nRestarting message forwarding loop...")
                await asyncio.sleep(10)  # Short delay before retry
            except Exception as e:
                print(f"Error in main loop: {str(e)}")
                await asyncio.sleep(5)  # Wait before retrying

if __name__ == "__main__":
    # Make sure sessions directory exists
    os.makedirs('sessions', exist_ok=True)

    try:
        print("Starting bot with database integration...")
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        print(f"An unexpected error occurred: {str(e)}")