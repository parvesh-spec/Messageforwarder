from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
import asyncio
import os
from flask import Flask
from datetime import datetime

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

print("Initializing bot...")

async def get_user_channels(phone):
    """Fetch user's channels directly from Telegram"""
    try:
        client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            print(f"Client not authorized for user {phone}")
            return None, None

        channels = []
        async for dialog in client.iter_dialogs():
            if dialog.is_channel:
                channels.append({
                    'id': dialog.id,
                    'name': dialog.name,
                    'username': dialog.entity.username if hasattr(dialog.entity, 'username') else None
                })

        await client.disconnect()
        return channels

    except Exception as e:
        print(f"Error fetching channels: {e}")
        return None, None


async def forward_messages():
    try:
        print("\n--- Starting message forwarding loop ---")
        #Get all active bot configurations (this part is removed since we are not using database)

        #This is replaced by fetching directly from telegram
        #bot_configs = BotConfig.query.filter_by(is_active=True).all()
        #print(f"Found {len(bot_configs)} active bot configurations")

        #Replace database fetch with environment variables or config file
        #For demonstration, we assume bot configurations are provided as environment variables
        bot_configs = os.environ.get('BOT_CONFIGS', '[]') #Example: '[{"user_phone": "+1234567890", "source_channels": ["1234567"], "dest_channels": ["7654321"]}]'
        try:
            bot_configs = eval(bot_configs)
        except (SyntaxError, NameError, TypeError):
            print("Invalid BOT_CONFIGS environment variable format.")
            return

        for config in bot_configs:
            user_phone = config.get('user_phone')
            source_channels = config.get('source_channels', [])
            dest_channels = config.get('dest_channels', [])
            
            if not user_phone or not source_channels or not dest_channels:
                print(f"Incomplete configuration for user {user_phone}")
                continue


            channels = await get_user_channels(user_phone)
            if channels is None:
                continue

            for source in source_channels:
                for dest in dest_channels:
                    await forward_messages_helper(user_phone, source, dest)



    except Exception as e:
        print(f"Error in forward_messages: {str(e)}")

async def forward_messages_helper(user_phone, source_channel, dest_channel):
    try:
        print(f"\nSetting up message forwarding from {source_channel} to {dest_channel}")
        client = TelegramClient(f"sessions/{user_phone}", API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            print(f"Client not authorized for user {user_phone}")
            return

        @client.on(events.NewMessage(chats=int(source_channel)))
        async def forward_handler(event):
            try:
                print(f"\nNew message received in source channel {source_channel}")
                # Forward the message
                forwarded = await client.forward_messages(
                    int(dest_channel),
                    event.message
                )
                if forwarded:
                    print(f"✓ Message successfully forwarded to {dest_channel}")
                else:
                    print(f"! Message forwarding failed to {dest_channel}")
            except Exception as e:
                print(f"Error in forward handler: {str(e)}")

        print(f"Message forwarding set up for user {user_phone}")
        await client.run_until_disconnected()

    except Exception as e:
        print(f"Error in forward_messages_helper: {str(e)}")


async def main():
    print("Starting Telegram bot service...")

    # Database initialization removed

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
        print("Starting bot...")
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        print(f"An unexpected error occurred: {str(e)}")