import sys
import logging
import json
import time
from threading import Thread

# Add stream handler to output logs to console
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)

# Rest of the imports
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
import asyncio
import os

# Message ID mapping dictionary
MESSAGE_IDS = {}  # Will store source_msg_id: destination_msg_id mapping

# Telegram API credentials
API_ID = int(os.getenv('API_ID', '27202142'))
API_HASH = os.getenv('API_HASH', 'db4dd0d95dc68d46b77518bf997ed165')

# Define source and destination channels
SOURCE_CHANNEL = None
DESTINATION_CHANNEL = None

# Define text replacement dictionary
TEXT_REPLACEMENTS = {}

def load_channel_config():
    global SOURCE_CHANNEL, DESTINATION_CHANNEL
    try:
        with open('channel_config.json', 'r') as f:
            config = json.load(f)
            SOURCE_CHANNEL = config.get('source_channel')
            DESTINATION_CHANNEL = config.get('destination_channel')
            logger.info(f"Loaded channel configuration - Source: {SOURCE_CHANNEL}, Destination: {DESTINATION_CHANNEL}")
    except FileNotFoundError:
        logger.warning("No channel configuration file found")
    except Exception as e:
        logger.error(f"Error loading channel configuration: {str(e)}")

def load_replacements():
    """Load text replacements from file"""
    global TEXT_REPLACEMENTS
    try:
        with open('text_replacements.json', 'r') as f:
            TEXT_REPLACEMENTS = json.load(f)
            logger.info(f"Loaded text replacements: {TEXT_REPLACEMENTS}")
    except FileNotFoundError:
        logger.warning("No text replacements file found")
        TEXT_REPLACEMENTS = {}
    except Exception as e:
        logger.error(f"Error loading text replacements: {str(e)}")
        TEXT_REPLACEMENTS = {}

def save_replacements():
    """Save text replacements to file"""
    try:
        with open('text_replacements.json', 'w') as f:
            json.dump(TEXT_REPLACEMENTS, f)
            logger.info(f"Saved text replacements: {TEXT_REPLACEMENTS}")
    except Exception as e:
        logger.error(f"Error saving text replacements: {str(e)}")

def config_monitor():
    while True:
        load_channel_config()
        load_replacements()
        save_replacements() #Save after loading in case of changes
        time.sleep(5)  # Check every 5 seconds

async def main():
    try:
        # Start config monitoring in background
        Thread(target=config_monitor, daemon=True).start()
        logger.info("Started channel configuration monitor")

        # Start the client
        logger.debug("Starting Telegram client...")
        client = TelegramClient('anon', API_ID, API_HASH, connection_retries=5, timeout=30)
        await client.start()

        # Check if already authorized
        if not await client.is_user_authorized():
            logger.error("Bot is not authorized. Please run the web interface first to authenticate.")
            return

        # Get information about yourself
        me = await client.get_me()
        logger.info(f"Successfully logged in as {me.first_name} (ID: {me.id})")

        @client.on(events.NewMessage())
        async def forward_handler(event):
            try:
                global SOURCE_CHANNEL, DESTINATION_CHANNEL

                # Add debug logging for received message
                logger.debug(f"Received message in channel: {event.chat_id}")
                logger.debug(f"SOURCE_CHANNEL configured as: {SOURCE_CHANNEL}")
                logger.debug(f"DESTINATION_CHANNEL configured as: {DESTINATION_CHANNEL}")
                logger.debug(f"Current TEXT_REPLACEMENTS: {TEXT_REPLACEMENTS}")

                # Skip if channels not configured
                if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                    logger.warning("Channels not configured yet")
                    return

                # Format source channel ID for comparison
                source_id = str(SOURCE_CHANNEL)
                if not source_id.startswith('-100'):
                    source_id = f"-100{source_id.lstrip('-')}"

                # Format event chat ID for comparison
                chat_id = str(event.chat_id)
                if not chat_id.startswith('-100'):
                    chat_id = f"-100{chat_id.lstrip('-')}"

                logger.debug(f"Comparing chat_id: {chat_id} with source_id: {source_id}")

                # Check if message is from source channel
                if chat_id != source_id:
                    logger.debug(f"Message not from source channel. Got: {chat_id}, Expected: {source_id}")
                    return

                logger.info(f"Processing message from source channel {source_id}")

                try:
                    # Format destination channel ID
                    dest_id = str(DESTINATION_CHANNEL)
                    if not dest_id.startswith('-100'):
                        dest_id = f"-100{dest_id.lstrip('-')}"

                    # Get destination channel entity
                    logger.debug(f"Getting entity for destination channel: {dest_id}")
                    dest_channel = await client.get_entity(int(dest_id))
                    logger.info(f"Destination channel found: {getattr(dest_channel, 'title', 'Unknown')}")

                    # Create a new message
                    message_text = event.message.text if event.message.text else ""
                    logger.debug(f"Original message text: {message_text}")

                    # Apply text replacements if any
                    if TEXT_REPLACEMENTS and message_text:
                        logger.debug("Applying text replacements...")
                        original_text = message_text
                        for original, replacement in TEXT_REPLACEMENTS.items():
                            if original in message_text:
                                message_text = message_text.replace(original, replacement)
                                logger.debug(f"Replaced '{original}' with '{replacement}'")
                        if original_text != message_text:
                            logger.info(f"Text modified from: '{original_text}' to: '{message_text}'")
                        else:
                            logger.debug("No replacements were applied to the text")


                    # Handle media
                    media = None
                    if event.message.media:
                        logger.info("Downloading media...")
                        try:
                            media = await event.message.download_media()
                            logger.info(f"Media downloaded: {media}")
                        except Exception as e:
                            logger.error(f"Failed to download media: {str(e)}")
                            return

                    # Send message
                    try:
                        if media:
                            logger.info("Sending message with media...")
                            sent_message = await client.send_file(
                                dest_channel,
                                media,
                                caption=message_text,
                                formatting_entities=event.message.entities
                            )
                            os.remove(media)  # Clean up
                            logger.info("Message with media sent successfully")
                        else:
                            logger.info("Sending text message...")
                            sent_message = await client.send_message(
                                dest_channel,
                                message_text,
                                formatting_entities=event.message.entities
                            )
                            logger.info("Text message sent successfully")

                        # Store message IDs mapping
                        MESSAGE_IDS[event.message.id] = sent_message.id
                        logger.debug(f"Stored message ID mapping: {event.message.id} → {sent_message.id}")

                    except Exception as e:
                        logger.error(f"Failed to send message: {str(e)}")
                        if media and os.path.exists(media):
                            os.remove(media)
                        return

                except ValueError as e:
                    logger.error(f"Failed to access destination channel: {str(e)}")
                    logger.error(f"Destination channel ID: {dest_id}")
                    return

            except Exception as e:
                logger.error(f"Error in forward handler: {str(e)}")
                logger.error(f"Error type: {type(e).__name__}")

        @client.on(events.MessageEdited())
        async def edit_handler(event):
            try:
                global SOURCE_CHANNEL, DESTINATION_CHANNEL
                logger.debug(f"Edit event received for message ID: {event.message.id}")
                logger.debug(f"Current SOURCE_CHANNEL: {SOURCE_CHANNEL}")
                logger.debug(f"Current DESTINATION_CHANNEL: {DESTINATION_CHANNEL}")

                # Skip if channels not configured
                if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                    logger.warning("Channels not configured yet")
                    return

                # Format IDs for comparison
                source_id = str(SOURCE_CHANNEL)
                if not source_id.startswith('-100'):
                    source_id = f"-100{source_id.lstrip('-')}"

                chat_id = str(event.chat_id)
                if not chat_id.startswith('-100'):
                    chat_id = f"-100{chat_id.lstrip('-')}"

                # Check if message is from source channel
                if chat_id != source_id:
                    logger.debug(f"Edit not from source channel. Got: {chat_id}, Expected: {source_id}")
                    return

                if event.message.id not in MESSAGE_IDS:
                    logger.info("❌ Original message mapping not found")
                    return

                dest_msg_id = MESSAGE_IDS[event.message.id]
                logger.info(f"Found destination message ID: {dest_msg_id}")

                # Get the edited message content
                message_text = event.message.text if event.message.text else ""

                # Apply text replacements if any
                if TEXT_REPLACEMENTS and message_text:
                    logger.debug("Applying text replacements to edited message...")
                    for original, replacement in TEXT_REPLACEMENTS.items():
                        message_text = message_text.replace(original, replacement)
                    logger.debug(f"Modified message text: {message_text}")

                try:
                    # Format destination channel ID
                    dest_id = str(DESTINATION_CHANNEL)
                    if not dest_id.startswith('-100'):
                        dest_id = f"-100{dest_id.lstrip('-')}"

                    # Get destination channel entity
                    channel = await client.get_entity(int(dest_id))

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