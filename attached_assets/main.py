from telethon import TelegramClient, events, sync, Button
from telethon.errors import SessionPasswordNeededError
import asyncio
import os
import re

# These example values won't work. You must get your own api_id and
# api_hash from https://my.telegram.org, under API Development.
API_ID = 27202142  # Replace with your API ID
API_HASH = 'db4dd0d95dc68d46b77518bf997ed165'  # Replace with your API hash

# The first parameter is the .session file name (absolute paths allowed)
client = TelegramClient('anon', API_ID, API_HASH, connection_retries=5, timeout=30)

# Define source and destination channels
# You will need to replace these with your actual channel IDs or usernames
SOURCE_CHANNEL = None  # Will be set during runtime
DESTINATION_CHANNEL = None  # Will be set during runtime

# Define text replacement dictionary
TEXT_REPLACEMENTS = {}  # Will be set during runtime

async def main():
    # Start the client
    print("Starting Telegram client...")
    await client.start()

    # Cache entities to minimize API calls
    print("Initializing entity cache to reduce API calls...")
    client._entity_cache = {}

    # Check if already authorized
    if not await client.is_user_authorized():
        print("You are not authorized. Let's log in.")
        phone = input(
            "Enter your phone number with country code (e.g., +11234567890): ")

        # Send code request
        await client.send_code_request(phone)

        # Get the verification code from the user
        verification_code = input("Enter the verification code you received: ")

        try:
            # Sign in with the code
            await client.sign_in(phone, verification_code)
        except SessionPasswordNeededError:
            # 2FA is enabled
            password = input(
                "You have two-factor authentication enabled. Please enter your password: "
            )
            await client.sign_in(password=password)

    # Get information about yourself
    me = await client.get_me()
    print(f"Successfully logged in as {me.first_name} (ID: {me.id})")

    # Get user input for channel settings
    global SOURCE_CHANNEL, DESTINATION_CHANNEL, TEXT_REPLACEMENTS

    print("\n----- Channel Auto-Forwarding Setup -----")

    # Function to validate channel access
    async def validate_channel(channel_input):
        try:
            # Clean up the input
            channel_input = channel_input.strip()

            # Simplify the process to avoid format confusion
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

    # Validate source channel
    print("Setting up source channel (where messages come from):")
    source_input = input("Enter the source channel username or ID (e.g., @channelname or -1001234567890): ")
    SOURCE_CHANNEL = await validate_channel(source_input)

    # Validate destination channel
    print("\nSetting up destination channel (where messages will be forwarded to):")
    destination_input = input("Enter the destination channel username or ID (e.g., @channelname or -1001234567890): ")
    DESTINATION_CHANNEL = await validate_channel(destination_input)

    # Ask if user wants to set up text replacements
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

        print(f"\nSet up {len(TEXT_REPLACEMENTS)} text replacements.")

    print(f"\nSet up forwarding from {SOURCE_CHANNEL} to {DESTINATION_CHANNEL}")
    if TEXT_REPLACEMENTS:
        print("Text replacements enabled:")
        for original, replacement in TEXT_REPLACEMENTS.items():
            print(f"- '{original}' → '{replacement}'")

    print("Monitoring for new messages. Press Ctrl+C to stop.")

    # Validate that we can access both channels before monitoring
    try:
        print("\nVerifying access to source channel...")
        source_entity = await client.get_entity(int(SOURCE_CHANNEL))
        print(f"✓ Successfully verified access to source channel: {getattr(source_entity, 'title', 'Unknown')}")

        print("Verifying access to destination channel...")
        dest_entity = await client.get_entity(int(DESTINATION_CHANNEL))
        print(f"✓ Successfully verified access to destination channel: {getattr(dest_entity, 'title', 'Unknown')}")
    except ValueError as e:
        print(f"❌ Error: {e}")
        print("\nPossible issues:")
        print("1. The bot is not a member of one of the channels")
        print("2. The channel ID format is incorrect")
        print("3. The channel doesn't exist")
        print("4. You don't have permission to access the channel")
        print("\nPlease restart the bot and enter valid channel IDs.")
        return

    # Listen for new messages in the source channel
    @client.on(events.NewMessage(chats=int(SOURCE_CHANNEL)))
    async def forward_handler(event):
        try:
            print(f"New message received in source channel: {SOURCE_CHANNEL}")

            # If we have text replacements and the message has text
            if TEXT_REPLACEMENTS and event.message.text:
                # Create a copy of the message to modify
                modified_message = event.message.text

                # Apply all text replacements
                for original, replacement in TEXT_REPLACEMENTS.items():
                    modified_message = modified_message.replace(original, replacement)

                # Only send as a new message if text was actually changed
                if modified_message != event.message.text:
                    print("Applying text replacements and sending as new message")

                    # Send the modified message text
                    sent = await client.send_message(DESTINATION_CHANNEL, modified_message)

                    # If the original message had media, download and send it with the new message
                    if event.message.media:
                        print("Message contains media, sending media with modified text")
                        # Download the media
                        downloaded_media = await event.message.download_media()
                        # Send the media with the modified text
                        await client.send_file(DESTINATION_CHANNEL, downloaded_media, caption=modified_message)
                        # Remove the temporary file
                        if os.path.exists(downloaded_media):
                            os.remove(downloaded_media)

                    message_preview = modified_message[:50] + "..." if len(modified_message) > 50 else modified_message
                    print(f"✓ Modified message sent: {message_preview}")
                    return

            # If no text replacements or text didn't change, forward the original message
            forwarded = await client.forward_messages(DESTINATION_CHANNEL, event.message)
            if forwarded:
                message_preview = event.message.text[:50] + "..." if event.message.text and len(event.message.text) > 50 else "Media or other content"
                print(f"✓ Message forwarded: {message_preview}")
            else:
                print("⚠️ Message forwarding failed for an unknown reason")
        except ValueError as e:
            print(f"❌ Error: Channel not found - {e}")
            print("Please restart the bot and enter the correct channel ID")
        except Exception as e:
            from telethon.errors.rpcerrorlist import FloodWaitError
            if isinstance(e, FloodWaitError):
                wait_time = e.seconds
                print(f"⚠️ Rate limit hit: Need to wait {wait_time} seconds")
                print(f"Sleeping for {wait_time} seconds and will retry after that")
                await asyncio.sleep(wait_time)
                print("Resuming after wait period")
                try:
                    # Try again after waiting
                    forwarded = await client.forward_messages(DESTINATION_CHANNEL, event.message)
                    if forwarded:
                        print(f"✓ Message forwarded after waiting")
                except Exception as retry_error:
                    print(f"❌ Error on retry: {retry_error}")
            else:
                print(f"❌ Error forwarding message: {e}")

    # Add a message to confirm we're monitoring
    print("\nMonitoring started. Bot is now running and watching for new messages.")
    print(f"SOURCE_CHANNEL: {SOURCE_CHANNEL}")
    print(f"DESTINATION_CHANNEL: {DESTINATION_CHANNEL}")
    if TEXT_REPLACEMENTS:
        print(f"TEXT_REPLACEMENTS: {TEXT_REPLACEMENTS}")

    # Listen for private messages to control the bot
    @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def command_handler(event):
        if event.raw_text.lower() == "/status":
            status_msg = f"Bot is running.\nForwarding from {SOURCE_CHANNEL} to {DESTINATION_CHANNEL}"
            if TEXT_REPLACEMENTS:
                status_msg += "\nText replacements enabled:"
                for original, replacement in TEXT_REPLACEMENTS.items():
                    status_msg += f"\n- '{original}' → '{replacement}'"
            await event.respond(status_msg)
        elif event.raw_text.lower().startswith("/replace "):
            # Command format: /replace original_text|replacement_text
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

    # Keep the script running
    await client.run_until_disconnected()


# Add a delay between API calls to avoid rate limits
async def delayed_api_call(coro, delay=0.5):
    """Wrapper to add delay between API calls to avoid rate limits"""
    await asyncio.sleep(delay)
    return await coro

if __name__ == "__main__":
    try:
        # Run the main function
        print("Starting bot with rate limit handling...")
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped by user.")
    except Exception as e:
        from telethon.errors.rpcerrorlist import FloodWaitError
        if isinstance(e, FloodWaitError):
            print(f"\nRate limit hit: Need to wait {e.seconds} seconds")
            print(f"Please restart the bot after {e.seconds} seconds.")
        else:
            print(f"An unexpected error occurred: {e}")