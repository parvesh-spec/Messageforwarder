from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
import asyncio
import os
from models import db, User, Channel, BotConfig
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

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
        # Get all active bot configurations
        bot_configs = BotConfig.query.filter_by(is_active=True).all()

        for config in bot_configs:
            print(f"Setting up forwarding for user {config.user_id}")

            # Get user's channels
            source_channels = Channel.query.filter_by(
                user_id=config.user_id,
                is_source=True
            ).all()

            destination_channels = Channel.query.filter_by(
                user_id=config.user_id,
                is_destination=True
            ).all()

            if not source_channels or not destination_channels:
                print(f"No channels configured for user {config.user_id}")
                continue

            # Get user's phone number for session
            user = User.query.get(config.user_id)
            if not user or not user.phone:
                print(f"No valid user found for ID {config.user_id}")
                continue

            try:
                # Create client for this user
                client = TelegramClient(f"sessions/{user.phone}", API_ID, API_HASH)
                await client.connect()

                if not await client.is_user_authorized():
                    print(f"Client not authorized for user {user.phone}")
                    continue

                print(f"Client authorized for user {user.phone}")

                # Set up message handler for each source channel
                for source in source_channels:
                    @client.on(events.NewMessage(chats=int(source.telegram_channel_id)))
                    async def forward_handler(event):
                        try:
                            print(f"\nNew message received in source channel {source.telegram_channel_id}")

                            for dest in destination_channels:
                                try:
                                    # Forward the message
                                    await client.forward_messages(
                                        int(dest.telegram_channel_id),
                                        event.message
                                    )
                                    print(f"Message forwarded to {dest.telegram_channel_id}")
                                except Exception as e:
                                    print(f"Error forwarding to {dest.telegram_channel_id}: {e}")

                        except Exception as e:
                            print(f"Error in forward handler: {e}")

                # Add message edit handler (from original code, adapted)
                for source in source_channels:
                    @client.on(events.MessageEdited(chats=int(source.telegram_channel_id)))
                    async def edit_handler(event):
                        try:
                            print(f"\nEdited message detected in source channel")
                            # Find corresponding message in destination
                            #This section needs significant adaptation to the database model.  Cannot complete without database schema.
                            pass
                        except Exception as e:
                            print(f"❌ Error in edit handler: {str(e)}")
                            print(f"Error type: {type(e).__name__}")
                            print(f"Full error details: {str(e)}")

                print(f"Message forwarding set up for user {user.phone}")

            except Exception as e:
                print(f"Error setting up client for user {user.phone}: {e}")
                continue

    except Exception as e:
        print(f"Error in forward_messages: {e}")

async def main():
    print("Starting Telegram bot service...")

    # Initialize database connection
    from flask import Flask
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    db.init_app(app)

    with app.app_context():
        db.create_all() #Ensure tables exist.
        while True:
            try:
                await forward_messages()
                print("Waiting for messages...")
                await asyncio.sleep(60)  # Check for new configurations every minute
            except Exception as e:
                print(f"Error in main loop: {e}")
                await asyncio.sleep(10)  # Wait before retrying


    # Command handler for bot control (from original code, adapted)
    #This section needs adaptation to the database model. Cannot complete without database schema.
    pass


if __name__ == "__main__":
    # Make sure sessions directory exists
    os.makedirs('sessions', exist_ok=True)

    try:
        print("Starting bot with database integration...")
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")