import sys
import logging
import asyncio
import os
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError

# Configure logging
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger = logging.getLogger(__name__)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)

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

# Global client and loop variables
client = None
loop = None

async def restart_client():
    """Restart the Telegram client with new configuration"""
    global client, loop
    try:
        if client:
            logger.info("Disconnecting existing client...")
            if client.is_connected():
                await client.disconnect()
            logger.info("Client disconnected")
    except Exception as e:
        logger.error(f"Error disconnecting client: {e}")

    try:
        logger.info("Starting new client...")
        # Initialize event loop if not exists
        if not loop:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

        client = TelegramClient('anon', API_ID, API_HASH, loop=loop)
        await client.connect()

        if not await client.is_user_authorized():
            logger.error("Bot is not authorized. Please run the web interface first to authenticate.")
            return

        # Log channel configuration
        logger.info(f"Client connected with channels - Source: {SOURCE_CHANNEL}, Destination: {DESTINATION_CHANNEL}")

        # Set up event handlers
        await setup_handlers()

        logger.info("Client restart complete")
    except Exception as e:
        logger.error(f"Error starting new client: {e}")
        raise

async def setup_handlers():
    """Set up message and edit handlers"""
    @client.on(events.NewMessage())
    async def forward_handler(event):
        try:
            # Log all relevant information
            logger.debug(f"Received message in channel: {event.chat_id}")
            logger.debug(f"SOURCE_CHANNEL configured as: {SOURCE_CHANNEL}")
            logger.debug(f"DESTINATION_CHANNEL configured as: {DESTINATION_CHANNEL}")

            if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                logger.warning("Channels not configured yet")
                return

            # Format channel IDs for comparison
            source_id = str(SOURCE_CHANNEL).replace('-100', '').lstrip('-')
            chat_id = str(event.chat_id).replace('-100', '').lstrip('-')

            logger.debug(f"Comparing chat_id: {chat_id} with source_id: {source_id}")

            if chat_id != source_id:
                logger.debug(f"Message not from source channel. Got: {chat_id}, Expected: {source_id}")
                return

            logger.info(f"Processing message from source channel {source_id}")

            # Format destination channel ID
            dest_id = str(DESTINATION_CHANNEL)
            if not dest_id.startswith('-100'):
                dest_id = f"-100{dest_id.lstrip('-')}"

            try:
                # Get destination channel entity
                dest_channel = await client.get_entity(int(dest_id))
                logger.info(f"Destination channel found: {getattr(dest_channel, 'title', 'Unknown')}")

                # Process message
                message_text = event.message.text if event.message.text else ""
                logger.debug(f"Original message text: {message_text}")

                # Apply text replacements
                if TEXT_REPLACEMENTS and message_text:
                    logger.debug("Applying text replacements...")
                    for original, replacement in TEXT_REPLACEMENTS.items():
                        message_text = message_text.replace(original, replacement)
                    logger.debug(f"Modified message text: {message_text}")

                # Forward message
                if event.message.media:
                    logger.info("Sending message with media...")
                    media = await event.message.download_media()
                    sent_message = await client.send_file(
                        dest_channel,
                        media,
                        caption=message_text,
                        formatting_entities=event.message.entities
                    )
                    os.remove(media)
                    logger.info("Message with media sent successfully")
                else:
                    logger.info("Sending text message...")
                    sent_message = await client.send_message(
                        dest_channel,
                        message_text,
                        formatting_entities=event.message.entities
                    )
                    logger.info("Text message sent successfully")

                MESSAGE_IDS[event.message.id] = sent_message.id
                logger.debug(f"Stored message ID mapping: {event.message.id} → {sent_message.id}")

            except Exception as e:
                logger.error(f"Failed to process message: {str(e)}")
                logger.error(f"Error type: {type(e).__name__}")
                return

        except Exception as e:
            logger.error(f"Error in forward handler: {str(e)}")
            logger.error(f"Error type: {type(e).__name__}")

    @client.on(events.MessageEdited())
    async def edit_handler(event):
        try:
            if not SOURCE_CHANNEL or not DESTINATION_CHANNEL:
                return

            # Format channel IDs
            source_id = str(SOURCE_CHANNEL).replace('-100', '').lstrip('-')
            chat_id = str(event.chat_id).replace('-100', '').lstrip('-')


            if chat_id != source_id:
                return

            if event.message.id not in MESSAGE_IDS:
                logger.info("Original message mapping not found")
                return

            dest_msg_id = MESSAGE_IDS[event.message.id]
            logger.info(f"Found destination message ID: {dest_msg_id}")

            # Process edited message
            message_text = event.message.text if event.message.text else ""
            if TEXT_REPLACEMENTS and message_text:
                for original, replacement in TEXT_REPLACEMENTS.items():
                    message_text = message_text.replace(original, replacement)

            # Update message
            try:
                channel = await client.get_entity(int(DESTINATION_CHANNEL))
                await client.edit_message(
                    channel,
                    dest_msg_id,
                    text=message_text,
                    formatting_entities=event.message.entities
                )
                logger.info("Message updated successfully")
            except Exception as e:
                logger.error(f"Error editing message: {str(e)}")

        except Exception as e:
            logger.error(f"Error in edit handler: {str(e)}")

async def main():
    global client, loop
    try:
        # Initialize event loop
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        client = TelegramClient('anon', API_ID, API_HASH, loop=loop)
        await client.start()

        if not await client.is_user_authorized():
            logger.error("Bot is not authorized. Please run the web interface first to authenticate.")
            return

        me = await client.get_me()
        logger.info(f"Successfully logged in as {me.first_name} (ID: {me.id})")

        await setup_handlers()

        logger.info("\nBot is running and monitoring for new messages and edits.")
        logger.info(f"Source channel: {SOURCE_CHANNEL}")
        logger.info(f"Destination channel: {DESTINATION_CHANNEL}")

        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Critical error in main function: {str(e)}")
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nBot stopped by user.")
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")