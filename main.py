from telethon import TelegramClient, events, sync, Button
from telethon.errors import SessionPasswordNeededError
import asyncio
import os
import re

# These example values won't work. You must get your own api_id and
# api_hash from https://my.telegram.org, under API Development.
API_ID = int(os.getenv('API_ID', '27202142'))  # Replace with your API ID
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')  # Replace with your API hash

# The first parameter is the .session file name (absolute paths allowed)
client = TelegramClient('anon', API_ID, API_HASH, connection_retries=5, timeout=30)

# Define source and destination channels
SOURCE_CHANNEL = None  # Will be set during runtime
DESTINATION_CHANNEL = None  # Will be set during runtime

# Define text replacement dictionary
TEXT_REPLACEMENTS = {}  # Will be set during runtime

async def main():
    # Start the client
    print("Starting Telegram client...")
    await client.start()

    # Check if already authorized
    if not await client.is_user_authorized():
        print("You are not authorized. Let's log in.")
        phone = input("Enter your phone number with country code (e.g., +11234567890): ")
        await client.send_code_request(phone)
        verification_code = input("Enter the verification code you received: ")

        try:
            await client.sign_in(phone, verification_code)
        except SessionPasswordNeededError:
            password = input("Two-factor authentication is enabled. Please enter your password: ")
            await client.sign_in(password=password)

    # Get information about yourself
    me = await client.get_me()
    print(f"Successfully logged in as {me.first_name} (ID: {me.id})")

    # Get user input for channel settings
    global SOURCE_CHANNEL, DESTINATION_CHANNEL, TEXT_REPLACEMENTS
    print("\n----- Channel Auto-Forwarding Setup -----")

    async def validate_channel(channel_input):
        try:
            print(f"\nValidating channel input: {channel_input}")
            channel_input = channel_input.strip()

            # Handle different channel ID formats
            if channel_input.isdigit() or (channel_input.startswith('-') and channel_input[1:].isdigit()):
                # Remove any existing -100 prefix and clean the ID
                clean_id = channel_input.replace('-100', '').lstrip('-')
                channel_input = f"-100{clean_id}"
                print(f"Formatted channel ID: {channel_input}")

                try:
                    print("Attempting to access channel...")
                    channel_entity = await client.get_entity(int(channel_input))
                    channel_name = getattr(channel_entity, 'title', 'Unknown')
                    print(f"✓ Successfully verified channel: {channel_name}")
                    return channel_input
                except ValueError as e:
                    print(f"❌ Error accessing channel: {str(e)}")
                    print("Please check:")
                    print("1. You are a member of the channel")
                    print("2. The channel ID is correct")
                    print("3. Your account has permission to access it")
                    retry = input("\nWould you like to try another channel ID? (y/n): ")
                    if retry.lower() == 'y':
                        return await validate_channel(input("Enter channel identifier: "))
                    return channel_input

            # For usernames starting with @
            elif channel_input.startswith('@'):
                try:
                    print("Attempting to access channel via username...")
                    entity = await client.get_entity(channel_input)
                    print(f"✓ Successfully found channel: {entity.title if hasattr(entity, 'title') else channel_input}")
                    return channel_input
                except Exception as e:
                    print(f"❌ Error accessing channel via username: {str(e)}")

            # Ask if it's a private channel
            is_private = input("Is this a private channel? (y/n): ")
            if is_private.lower() == 'y':
                print("\nEnter the channel ID with proper format (should start with -100)")
                direct_id = input("Channel ID: ")
                clean_id = direct_id.replace('-100', '').lstrip('-')
                formatted_id = f"-100{clean_id}"
                print(f"Using channel ID: {formatted_id}")
                return formatted_id

            print("\nChannel format guide:")
            print("- For private channels: use ID with -100 prefix (e.g., -1001234567890)")
            print("- For public channels: use username with @ (e.g., @channelname)")
            print("- Make sure you are a member of the channel")

            retry = input("\nWould you like to try again? (y/n): ")
            if retry.lower() == 'y':
                return await validate_channel(input("Enter channel identifier: "))
            return channel_input

        except Exception as e:
            print(f"Validation error: {str(e)}")
            print("Stack trace:", e.__traceback__)
            return await validate_channel(input("Enter channel identifier again: "))

    # Validate source channel
    print("\nSetting up source channel (where messages come from):")
    source_input = input("Enter the source channel username or ID (e.g., @channelname or -1001234567890): ")
    SOURCE_CHANNEL = await validate_channel(source_input)

    # Validate destination channel
    print("\nSetting up destination channel (where messages will be forwarded to):")
    destination_input = input("Enter the destination channel username or ID (e.g., @channelname or -1001234567890): ")
    DESTINATION_CHANNEL = await validate_channel(destination_input)

    # Set up text replacements
    setup_replacements = input("\nDo you want to set up text replacements in forwarded messages? (y/n): ")
    if setup_replacements.lower() == 'y':
        print("\n----- Text Replacement Setup -----")
        print("You can replace specific text in messages before forwarding them.")
        print("For example, replace 'Hello' with 'Hi' in all messages.")

        while True:
            original_text = input("\nEnter the original text to replace (or leave empty to finish): ")
            if not original_text:
                break
            replacement_text = input(f"Enter the text to replace '{original_text}' with: ")
            TEXT_REPLACEMENTS[original_text] = replacement_text
            print(f"✓ Added replacement: '{original_text}' → '{replacement_text}'")

    print(f"\nForwarding setup complete:")
    print(f"Source: {SOURCE_CHANNEL}")
    print(f"Destination: {DESTINATION_CHANNEL}")
    if TEXT_REPLACEMENTS:
        print("Text replacements:")
        for original, replacement in TEXT_REPLACEMENTS.items():
            print(f"- '{original}' → '{replacement}'")

    # Message handler for forwarding
    @client.on(events.NewMessage(chats=int(SOURCE_CHANNEL)))
    async def forward_handler(event):
        try:
            print(f"\nNew message received in source channel")
            print(f"Source channel ID: {SOURCE_CHANNEL}")
            print(f"Destination channel ID: {DESTINATION_CHANNEL}")

            # Format destination channel ID correctly
            try:
                # First, remove any -100 prefix and clean the ID
                clean_id = DESTINATION_CHANNEL.replace('-100', '').lstrip('-')
                # Add the -100 prefix exactly once
                formatted_dest = f"-100{clean_id}"
                print(f"Formatted destination ID: {formatted_dest}")

                try:
                    # First get full entity to verify access
                    print("Attempting to verify channel access...")
                    channel = await client.get_entity(int(formatted_dest))
                    print(f"✓ Channel access verified: {getattr(channel, 'title', 'Unknown')}")

                    # Create a new message
                    message_text = event.message.text if event.message.text else ""

                    # Apply text replacements if any
                    if TEXT_REPLACEMENTS and message_text:
                        print("Applying text replacements...")
                        original_text = message_text
                        for original, replacement in TEXT_REPLACEMENTS.items():
                            message_text = message_text.replace(original, replacement)
                        if original_text != message_text:
                            print("✓ Text replacements applied")

                    # Handle media
                    media = None
                    if event.message.media:
                        print("Downloading media...")
                        try:
                            media = await event.message.download_media()
                            print("✓ Media downloaded successfully")
                        except Exception as e:
                            print(f"❌ Error downloading media: {str(e)}")
                            return

                    # Send message
                    try:
                        if media:
                            print("Sending message with media...")
                            await client.send_file(
                                channel,  # Use the verified channel entity
                                media,
                                caption=message_text,
                                formatting_entities=event.message.entities
                            )
                            os.remove(media)  # Clean up
                            print("✓ Message with media sent successfully")
                        else:
                            print("Sending text message...")
                            await client.send_message(
                                channel,  # Use the verified channel entity
                                message_text,
                                formatting_entities=event.message.entities
                            )
                            print("✓ Text message sent successfully")

                    except Exception as e:
                        print(f"❌ Error sending message: {str(e)}")
                        if media and os.path.exists(media):
                            os.remove(media)  # Clean up on error
                        return

                except ValueError as e:
                    print("❌ Error: Could not access destination channel.")
                    print("Please verify:")
                    print("1. The bot/account is a member of the channel")
                    print("2. The channel ID is correct")
                    print("3. You have permission to post in the channel")
                    print(f"Full error: {str(e)}")
                    return

            except Exception as e:
                print(f"❌ Error with channel ID formatting: {str(e)}")
                print(f"Raw destination ID: {DESTINATION_CHANNEL}")
                return

        except Exception as e:
            print(f"❌ Error in message handler: {str(e)}")
            print(f"Error type: {type(e).__name__}")
            print(f"Full error details: {str(e)}")

    # Command handler for bot control
    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def command_handler(event):
        if event.raw_text.lower() == "/status":
            status_msg = f"Bot is running\nForwarding from {SOURCE_CHANNEL} to {DESTINATION_CHANNEL}"
            if TEXT_REPLACEMENTS:
                status_msg += "\nText replacements:"
                for original, replacement in TEXT_REPLACEMENTS.items():
                    status_msg += f"\n- '{original}' → '{replacement}'"
            await event.respond(status_msg)
        elif event.raw_text.lower().startswith("/replace "):
            try:
                parts = event.raw_text[9:].split('|', 1)
                if len(parts) != 2:
                    await event.respond("Invalid format. Use: /replace original_text|replacement_text")
                    return
                original, replacement = parts
                TEXT_REPLACEMENTS[original] = replacement
                await event.respond(f"✓ Added replacement: '{original}' → '{replacement}'")
            except Exception as e:
                await event.respond(f"Error adding replacement: {e}")
        elif event.raw_text.lower() == "/replacements":
            if not TEXT_REPLACEMENTS:
                await event.respond("No text replacements set up.")
            else:
                msg = "Current text replacements:"
                for original, replacement in TEXT_REPLACEMENTS.items():
                    msg += f"\n- '{original}' → '{replacement}'"
                await event.respond(msg)
        elif event.raw_text.lower() == "/clearreplacements":
            TEXT_REPLACEMENTS.clear()
            await event.respond("✓ All text replacements cleared.")
        elif event.raw_text.lower() == "/help":
            help_msg = "Commands:\n"
            help_msg += "/status - Check bot status\n"
            help_msg += "/replace original|replacement - Add a text replacement\n"
            help_msg += "/replacements - List all current replacements\n"
            help_msg += "/clearreplacements - Clear all text replacements\n"
            help_msg += "/help - Show this help message"
            await event.respond(help_msg)
        else:
            await event.respond("I'm a channel forwarding bot. Use /help to see available commands.")

    print("\nBot is running and monitoring for new messages.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")