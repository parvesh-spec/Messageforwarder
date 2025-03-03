from telethon import TelegramClient, events, sync, Button
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import MessageEntityBold, MessageEntityItalic, MessageEntityCode
import asyncio
import os
import re

# API credentials 
API_ID = 27202142  # Replace with your API ID
API_HASH = 'db4dd0d95dc68d46b77518bf997ed165'  # Replace with your API hash

# Initialize client
client = TelegramClient('anon', API_ID, API_HASH, connection_retries=5, timeout=30)

# Define source and destination channels
SOURCE_CHANNEL = None  # Will be set during runtime
DESTINATION_CHANNEL = None  # Will be set during runtime

# Define text replacement dictionary
TEXT_REPLACEMENTS = {}  # Will be set during runtime

async def copy_message_formatting(message):
    """Preserve message formatting by recreating entity attributes"""
    if not message.entities:
        return message.text

    text = message.text
    formatted_text = text
    offset_change = 0

    for entity in sorted(message.entities, key=lambda e: e.offset):
        start = entity.offset + offset_change
        end = start + entity.length

        if isinstance(entity, MessageEntityBold):
            formatted_text = formatted_text[:start] + f"**{formatted_text[start:end]}**" + formatted_text[end:]
            offset_change += 4
        elif isinstance(entity, MessageEntityItalic):
            formatted_text = formatted_text[:start] + f"*{formatted_text[start:end]}*" + formatted_text[end:]
            offset_change += 2
        elif isinstance(entity, MessageEntityCode):
            formatted_text = formatted_text[:start] + f"`{formatted_text[start:end]}`" + formatted_text[end:]
            offset_change += 2

    return formatted_text

async def copy_message(message, destination):
    """Copy a message to destination while preserving all attributes"""
    try:
        # Handle text with formatting
        if message.text:
            text = await copy_message_formatting(message)

            # Apply text replacements if configured
            if TEXT_REPLACEMENTS:
                for original, replacement in TEXT_REPLACEMENTS.items():
                    text = text.replace(original, replacement)
        else:
            text = ""

        # Handle media
        if message.media:
            # Download media to temporary file
            downloaded_media = await message.download_media()

            # Send media with caption
            sent_message = await client.send_file(
                destination,
                downloaded_media,
                caption=text,
                parse_mode='md'
            )

            # Cleanup temporary file
            if os.path.exists(downloaded_media):
                os.remove(downloaded_media)

            return sent_message
        else:
            # Send text-only message
            return await client.send_message(
                destination,
                text,
                parse_mode='md'
            )

    except Exception as e:
        print(f"Error copying message: {e}")
        return None

async def validate_channel(channel_input):
    """Validate and format channel identifier"""
    try:
        # Clean up the input
        channel_input = channel_input.strip()
        print(f"\nUsing channel identifier: {channel_input}")

        # Simple channel ID format correction for common mistakes
        if channel_input.isdigit():
            # If only digits are entered, assume it's a channel ID and add -100 prefix
            channel_input = f"-100{channel_input}"
            print(f"Added -100 prefix: {channel_input}")
        elif channel_input.startswith('-') and channel_input[1:].isdigit():
            # If it starts with just a single dash, add -100 prefix
            channel_input = f"-100{channel_input[1:]}"
            print(f"Added -100 prefix: {channel_input}")
        elif channel_input.startswith('-1002'):
            # Fix common typo
            channel_input = '-100' + channel_input[5:]
            print(f"Fixed format to: {channel_input}")

        # For channel IDs starting with -100, verify if we can access it
        if channel_input.startswith('-100'):
            print(f"Using channel ID: {channel_input}")
            try:
                # Try to access the channel to verify ID is correct
                channel_entity = await client.get_entity(int(channel_input))
                channel_name = getattr(channel_entity, 'title', 'Unknown')
                print(f"✓ Successfully verified channel: {channel_name}")
                return channel_input
            except ValueError:
                print("❌ Cannot access this channel. Make sure:")
                print("1. You are a member of the channel")
                print("2. The channel ID is correct")
                print("3. Your account has permission to access it")
                retry = input("\nWould you like to try another channel ID? (y/n): ")
                if retry.lower() == 'y':
                    return await validate_channel(input("Enter channel identifier: "))
                else:
                    print("Using the provided ID anyway. This may cause errors later.")
                    return channel_input

        # For usernames, try direct access
        if channel_input.startswith('@'):
            try:
                entity = await client.get_entity(channel_input)
                print(f"✓ Successfully found channel: {entity.title if hasattr(entity, 'title') else channel_input}")
                return channel_input
            except Exception as e:
                print(f"❌ Error accessing channel: {str(e)}")

        # If we get here, ask if it's a private channel
        is_private = input("Is this a private channel? (y/n): ")
        if is_private.lower() == 'y':
            try:
                # For private channels, list available channels to select from
                print("\nListing your recent channels...")

                # Use a smaller limit to avoid rate limiting
                dialogs = await client.get_dialogs(limit=10)

                # Show a list of available channels
                print("\nYour recent channels:")
                for i, dialog in enumerate(dialogs):
                    channel_type = "Channel" if hasattr(dialog.entity, 'broadcast') and dialog.entity.broadcast else "Group/Chat"
                    peer_id = f"-100{dialog.entity.id}" if hasattr(dialog.entity, 'id') else "Unknown ID"
                    print(f"{i+1}. {dialog.name} ({channel_type}, ID: {peer_id})")

                # Let user select from the list
                print("\nSelect a channel from the list above or enter an ID directly.")
                selection = input("Enter a number or channel ID: ")

                # Process selection
                if selection.isdigit() and 1 <= int(selection) <= len(dialogs):
                    # User selected from the list
                    selected_dialog = dialogs[int(selection)-1]
                    peer_id = f"-100{selected_dialog.entity.id}"
                    print(f"Selected: {selected_dialog.name} (ID: {peer_id})")
                    return peer_id
                elif selection.startswith('-100'):
                    # User entered an ID with proper format
                    print(f"Using direct channel ID: {selection}")
                    return selection
                elif selection.isdigit() or (selection.startswith('-') and selection[1:].isdigit()):
                    # User entered a raw ID, add proper prefix
                    corrected_id = f"-100{selection.replace('-', '')}"
                    print(f"Using formatted channel ID: {corrected_id}")
                    return corrected_id
            except Exception as e:
                print(f"Error listing dialogs: {e}")
                print("Due to Telegram rate limits, we'll use direct ID entry.")

            # Direct ID entry as fallback
            print("\nEnter the channel ID with proper format (should start with -100)")
            direct_id = input("Channel ID: ")

            # Ensure proper format
            if direct_id.isdigit():
                direct_id = f"-100{direct_id}"
            elif direct_id.startswith('-') and not direct_id.startswith('-100'):
                direct_id = f"-100{direct_id[1:]}"

            print(f"Using channel ID: {direct_id}")
            return direct_id

        # If all else fails, try direct entity lookup
        try:
            entity = await client.get_entity(channel_input)
            print(f"✓ Found channel: {entity.title if hasattr(entity, 'title') else channel_input}")
            return channel_input
        except Exception as e:
            print(f"❌ Error: {str(e)}")

        print("\nChannel format guide:")
        print("- For private channels: use ID with -100 prefix (e.g., -1001234567890)")
        print("- For public channels: use username with @ (e.g., @channelname)")
        print("- Make sure you are a member of the channel")

        retry = input("\nEnter a different channel identifier: ")
        if retry:
            return await validate_channel(retry)
        return await validate_channel(input("Enter channel identifier: "))

    except Exception as e:
        print(f"Validation error: {e}")
        return await validate_channel(input("Enter channel identifier again: "))

async def main():
    # Start the client
    print("Starting Telegram client...")
    await client.start()

    # Initialize entity cache
    print("Initializing entity cache to reduce API calls...")
    client._entity_cache = {}

    # Check authorization
    if not await client.is_user_authorized():
        print("You are not authorized. Let's log in.")
        phone = input("Enter your phone number with country code (e.g., +11234567890): ")
        await client.send_code_request(phone)
        verification_code = input("Enter the verification code you received: ")

        try:
            await client.sign_in(phone, verification_code)
        except SessionPasswordNeededError:
            password = input("Two-factor authentication enabled. Please enter your password: ")
            await client.sign_in(password=password)

    # Get user info
    me = await client.get_me()
    print(f"Successfully logged in as {me.first_name} (ID: {me.id})")

    # Channel setup
    global SOURCE_CHANNEL, DESTINATION_CHANNEL

    print("\n----- Channel Auto-Forwarding Setup -----")

    # Set up source and destination channels
    print("\nSetting up source channel:")
    SOURCE_CHANNEL = await validate_channel(input("Enter source channel ID/username: "))

    print("\nSetting up destination channel:")
    DESTINATION_CHANNEL = await validate_channel(input("Enter destination channel ID/username: "))

    # Text replacement setup
    setup_replacements = input("\nSet up text replacements? (y/n): ")
    if setup_replacements.lower() == 'y':
        print("\n----- Text Replacement Setup -----")
        while True:
            original = input("\nEnter text to replace (empty to finish): ")
            if not original:
                break
            replacement = input(f"Replace '{original}' with: ")
            TEXT_REPLACEMENTS[original] = replacement
            print(f"✓ Added: '{original}' → '{replacement}'")

    print("\nMonitoring for new messages. Press Ctrl+C to stop.")

    # Message handler for source channel
    @client.on(events.NewMessage(chats=int(SOURCE_CHANNEL) if SOURCE_CHANNEL.startswith('-100') else SOURCE_CHANNEL))
    async def message_handler(event):
        try:
            print(f"\nNew message received in source channel")

            # Copy message to destination
            sent_message = await copy_message(event.message, DESTINATION_CHANNEL)

            if sent_message:
                preview = event.message.text[:50] + "..." if event.message.text and len(event.message.text) > 50 else "Media message"
                print(f"✓ Message copied successfully: {preview}")
            else:
                print("⚠️ Failed to copy message")

        except Exception as e:
            from telethon.errors.rpcerrorlist import FloodWaitError
            if isinstance(e, FloodWaitError):
                wait_time = e.seconds
                print(f"⚠️ Rate limit hit: Need to wait {wait_time} seconds")
                await asyncio.sleep(wait_time)
                try:
                    sent_message = await copy_message(event.message, DESTINATION_CHANNEL)
                    print("✓ Message sent after waiting")
                except Exception as retry_error:
                    print(f"❌ Error on retry: {retry_error}")
            else:
                print(f"❌ Error handling message: {e}")

    # Command handlers for bot control
    @client.on(events.NewMessage(pattern='/status'))
    async def status_handler(event):
        if event.is_private:
            status = f"Bot Status:\nSource: {SOURCE_CHANNEL}\nDestination: {DESTINATION_CHANNEL}\n"
            if TEXT_REPLACEMENTS:
                status += "Text replacements:\n"
                for original, replacement in TEXT_REPLACEMENTS.items():
                    status += f"'{original}' → '{replacement}'\n"
            else:
                status += "No text replacements configured"
            await event.respond(status)

    @client.on(events.NewMessage(pattern='/replacements'))
    async def replacements_handler(event):
        if event.is_private:
            if not TEXT_REPLACEMENTS:
                await event.respond("No active replacements")
            else:
                msg = "Active replacements:\n"
                for original, replacement in TEXT_REPLACEMENTS.items():
                    msg += f"'{original}' → '{replacement}'\n"
                await event.respond(msg)

    @client.on(events.NewMessage(pattern='^/replace (.+)\\|(.+)$'))
    async def add_replacement_handler(event):
        if event.is_private:
            try:
                original, replacement = event.pattern_match.groups()
                TEXT_REPLACEMENTS[original.strip()] = replacement.strip()
                await event.respond(f"✓ Added replacement: '{original}' → '{replacement}'")
            except Exception as e:
                await event.respond(f"❌ Error adding replacement: {e}")

    @client.on(events.NewMessage(pattern='/help'))
    async def help_handler(event):
        if event.is_private:
            help_text = """Available commands:
/status - Show bot status and active replacements
/replacements - List all text replacements
/replace text|replacement - Add new text replacement
/help - Show this help message"""
            await event.respond(help_text)

    # Keep the bot running
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user")
    except Exception as e:
        print(f"Fatal error: {e}")