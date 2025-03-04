from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
import asyncio
import os
import json

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

print("Initializing bot...")

# Store active forwarding configurations
active_forwards = {}

async def start_forwarding(user_phone, source_channel, dest_channel):
    try:
        print(f"\nStarting forwarding for {user_phone}: {source_channel} -> {dest_channel}")

        # Create or get existing client
        client = TelegramClient(f"sessions/{user_phone}", API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            print(f"Client not authorized for user {user_phone}")
            return

        # Set up event handler for new messages
        @client.on(events.NewMessage(chats=int(source_channel)))
        async def forward_handler(event):
            try:
                print(f"\nNew message received in source channel {source_channel}")
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

        # Store the client for this configuration
        config_key = f"{user_phone}_{source_channel}_{dest_channel}"
        active_forwards[config_key] = client

        print(f"✓ Forwarding set up successfully for {config_key}")

        # Keep the client running
        await client.run_until_disconnected()

    except Exception as e:
        print(f"Error in start_forwarding: {str(e)}")
        if config_key in active_forwards:
            del active_forwards[config_key]

async def stop_forwarding(user_phone, source_channel, dest_channel):
    try:
        config_key = f"{user_phone}_{source_channel}_{dest_channel}"
        if config_key in active_forwards:
            client = active_forwards[config_key]
            await client.disconnect()
            del active_forwards[config_key]
            print(f"✓ Forwarding stopped for {config_key}")
    except Exception as e:
        print(f"Error stopping forwarding: {str(e)}")

async def check_channels(phone, channel_id):
    """Verify if a channel exists and is accessible"""
    try:
        client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            return False

        try:
            channel = await client.get_entity(int(channel_id))
            return True if channel else False
        except:
            return False
        finally:
            await client.disconnect()
    except Exception as e:
        print(f"Error checking channel: {str(e)}")
        return False

async def get_user_channels(phone):
    """Fetch user's channels directly from Telegram"""
    try:
        client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            print(f"Client not authorized for user {phone}")
            return []

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
        return []

# Main loop to handle new forwarding requests
async def main():
    print("Starting Telegram bot service...")
    os.makedirs('sessions', exist_ok=True)

    while True:
        try:
            # Check for new forwarding requests from a file or environment variable
            config_str = os.environ.get('ACTIVE_FORWARDS', '[]')
            try:
                configs = json.loads(config_str)
                for config in configs:
                    phone = config.get('phone')
                    source = config.get('source')
                    dest = config.get('dest')

                    if phone and source and dest:
                        config_key = f"{phone}_{source}_{dest}"
                        if config_key not in active_forwards:
                            # Start new forwarding
                            asyncio.create_task(start_forwarding(phone, source, dest))
            except json.JSONDecodeError:
                print("Invalid ACTIVE_FORWARDS format")

            await asyncio.sleep(10)  # Check for new configurations every 10 seconds

        except Exception as e:
            print(f"Error in main loop: {str(e)}")
            await asyncio.sleep(5)  # Wait before retrying

if __name__ == "__main__":
    try:
        print("Starting bot...")
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        print(f"An unexpected error occurred: {str(e)}")