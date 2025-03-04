from telethon import TelegramClient, events, sync, Button
from telethon.errors import SessionPasswordNeededError
import asyncio
import os
import re
import logging

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Add message ID mapping dictionary
MESSAGE_IDS = {}  # Will store source_msg_id: destination_msg_id mapping

# These example values won't work. You must get your own api_id and
# api_hash from https://my.telegram.org, under API Development.
API_ID = int(os.getenv('API_ID', '27202142'))  # Replace with your API ID
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')  # Replace with your API hash

# Define source and destination channels - will be set by app.py
SOURCE_CHANNEL = None
DESTINATION_CHANNEL = None

# Define text replacement dictionary
TEXT_REPLACEMENTS = {}  # Will be populated during runtime

async def main():
    try:
        # Start the client
        logger.info("Starting Telegram client...")
        client = TelegramClient('anon', API_ID, API_HASH, connection_retries=5, timeout=30)
        await client.start()

        # Check if already authorized
        if not await client.is_user_authorized():
            logger.error("Bot is not authorized. Please run the web interface first to authenticate.")
            return

        # Get information about yourself
        me = await client.get_me()
        logger.info(f"Successfully logged in as {me.first_name} (ID: {me.id})")

        # Message handler for forwarding
        @client.on(events.NewMessage())
        async def forward_handler(event):
            try:
                # Skip if channels not configured
                if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                    logger.info("Channels not configured yet")
                    return

                # Check if message is from source channel
                if str(event.chat_id) != str(SOURCE_CHANNEL):
                    return

                logger.info(f"\nNew message received in source channel")
                logger.info(f"Source channel ID: {SOURCE_CHANNEL}")
                logger.info(f"Destination channel ID: {DESTINATION_CHANNEL}")

                try:
                    # Get destination channel entity
                    channel = await client.get_entity(int(DESTINATION_CHANNEL))
                    logger.info(f"✓ Channel access verified: {getattr(channel, 'title', 'Unknown')}")

                    # Create a new message
                    message_text = event.message.text if event.message.text else ""

                    # Apply text replacements if any
                    if TEXT_REPLACEMENTS and message_text:
                        logger.info("Applying text replacements...")
                        for original, replacement in TEXT_REPLACEMENTS.items():
                            message_text = message_text.replace(original, replacement)

                    # Handle media
                    media = None
                    if event.message.media:
                        logger.info("Downloading media...")
                        try:
                            media = await event.message.download_media()
                            logger.info("✓ Media downloaded successfully")
                        except Exception as e:
                            logger.error(f"❌ Error downloading media: {str(e)}")
                            return

                    # Send message
                    try:
                        if media:
                            logger.info("Sending message with media...")
                            sent_message = await client.send_file(
                                channel,
                                media,
                                caption=message_text,
                                formatting_entities=event.message.entities
                            )
                            os.remove(media)  # Clean up
                            logger.info("✓ Message with media sent successfully")
                        else:
                            logger.info("Sending text message...")
                            sent_message = await client.send_message(
                                channel,
                                message_text,
                                formatting_entities=event.message.entities
                            )
                            logger.info("✓ Text message sent successfully")

                        # Store message IDs mapping
                        MESSAGE_IDS[event.message.id] = sent_message.id
                        logger.info(f"✓ Message ID mapping stored: {event.message.id} → {sent_message.id}")

                    except Exception as e:
                        logger.error(f"❌ Error sending message: {str(e)}")
                        if media and os.path.exists(media):
                            os.remove(media)  # Clean up on error
                        return

                except ValueError as e:
                    logger.error("❌ Error: Could not access destination channel.")
                    logger.error("Please verify:")
                    logger.error("1. The bot/account is a member of the channel")
                    logger.error("2. The channel ID is correct")
                    logger.error("3. You have permission to post in the channel")
                    logger.error(f"Full error: {str(e)}")
                    return

            except Exception as e:
                logger.error(f"❌ Error in message handler: {str(e)}")
                logger.error(f"Error type: {type(e).__name__}")
                logger.error(f"Full error details: {str(e)}")

        # Add message edit handler
        @client.on(events.MessageEdited())
        async def edit_handler(event):
            try:
                # Skip if channels not configured
                if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                    return

                # Check if message is from source channel
                if str(event.chat_id) != str(SOURCE_CHANNEL):
                    return

                logger.info(f"\nEdited message detected in source channel")
                if event.message.id not in MESSAGE_IDS:
                    logger.info("❌ Original message mapping not found")
                    return

                dest_msg_id = MESSAGE_IDS[event.message.id]
                logger.info(f"Found destination message ID: {dest_msg_id}")

                # Get the edited message content
                message_text = event.message.text if event.message.text else ""

                # Apply text replacements if any
                if TEXT_REPLACEMENTS and message_text:
                    logger.info("Applying text replacements to edited message...")
                    for original, replacement in TEXT_REPLACEMENTS.items():
                        message_text = message_text.replace(original, replacement)

                try:
                    # Get destination channel entity
                    channel = await client.get_entity(int(DESTINATION_CHANNEL))
                    # Edit the corresponding message
                    logger.info("Updating message in destination channel...")
                    await client.edit_message(
                        channel,
                        dest_msg_id,
                        text=message_text,
                        formatting_entities=event.message.entities
                    )
                    logger.info("✓ Message updated successfully")

                except Exception as e:
                    logger.error(f"❌ Error editing message: {str(e)}")
                    return

            except Exception as e:
                logger.error(f"❌ Error in edit handler: {str(e)}")
                logger.error(f"Error type: {type(e).__name__}")
                logger.error(f"Full error details: {str(e)}")

        # Command handler for bot control and text replacements
        @client.on(events.NewMessage(pattern=r'/start|/help'))
        async def command_handler(event):
            logger.info(f"Received command: {event.raw_text}")
            help_msg = "Available commands:\n"
            help_msg += "/status - Check bot status\n"
            help_msg += "/replace text|replacement - Add text replacement\n"
            help_msg += "/replacements - List text replacements\n"
            help_msg += "/clearreplacements - Clear all replacements"
            await event.respond(help_msg)

        @client.on(events.NewMessage(pattern=r'/status'))
        async def status_handler(event):
            logger.info("Status command received")
            status_msg = f"Bot is running\nForwarding from {SOURCE_CHANNEL} to {DESTINATION_CHANNEL}"
            if TEXT_REPLACEMENTS:
                status_msg += "\nActive replacements:"
                for original, replacement in TEXT_REPLACEMENTS.items():
                    status_msg += f"\n- '{original}' → '{replacement}'"
            await event.respond(status_msg)

        @client.on(events.NewMessage(pattern=r'/replace\s+([^|]+)\|(.+)'))
        async def replace_handler(event):
            try:
                original = event.pattern_match.group(1).strip()
                replacement = event.pattern_match.group(2).strip()
                TEXT_REPLACEMENTS[original] = replacement
                logger.info(f"Added replacement: '{original}' → '{replacement}'")
                await event.respond(f"✓ Added replacement: '{original}' → '{replacement}'")
            except Exception as e:
                logger.error(f"Error in replace handler: {e}")
                await event.respond("❌ Error: Use format /replace original|replacement")

        @client.on(events.NewMessage(pattern=r'/replacements'))
        async def list_replacements_handler(event):
            if not TEXT_REPLACEMENTS:
                await event.respond("No active replacements")
            else:
                msg = "Active replacements:"
                for original, replacement in TEXT_REPLACEMENTS.items():
                    msg += f"\n- '{original}' → '{replacement}'"
                await event.respond(msg)

        @client.on(events.NewMessage(pattern=r'/clearreplacements'))
        async def clear_replacements_handler(event):
            TEXT_REPLACEMENTS.clear()
            logger.info("Cleared all text replacements")
            await event.respond("✓ All replacements cleared")

        logger.info("\nBot is running and monitoring for new messages and edits.")
        logger.info(f"Source channel: {SOURCE_CHANNEL}")
        logger.info(f"Destination channel: {DESTINATION_CHANNEL}")
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Critical error in main function: {str(e)}")
        logger.error(f"Error type: {type(e).__name__}")
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nBot stopped by user.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")