# Telegram Auto-Forwarding Bot 📱

A simple but powerful Telegram bot that helps you forward messages between channels with some cool features!

## What Can This Bot Do? 🌟

- **Auto-Forward Messages**: Automatically sends messages from one channel to another
- **Clean Forwarding**: Messages appear as new posts, not as forwards
- **Word Replacement**: Change specific words in messages (like "Hello" to "Hi")
- **Edit Sync**: When you edit a message in the source channel, it updates in the destination too
- **Media Support**: Works with text, images, videos, and other files

## Quick Start 🚀

1. **Get Your Telegram API Details**
   - Visit https://my.telegram.org
   - Create an application
   - Save your `API_ID` and `API_HASH`

2. **Install What You Need**
   ```bash
   pip install telethon
   ```

3. **Start the Bot**
   ```bash
   python main.py
   ```

4. **Set Up Your Channels**
   - Enter source channel (where messages come from)
   - Enter destination channel (where messages should go)
   - Choose if you want to replace any words

## Basic Commands 🎮

- `/status` - See what the bot is doing
- `/replace oldword|newword` - Replace words in messages
- `/replacements` - See all word replacements
- `/clearreplacements` - Remove all replacements
- `/help` - Show all commands

## Need Help? 🔧

We have two guides to help you:
- [Complete Guide](docs/GUIDE.md) - Detailed setup and usage instructions
- [How It Works](docs/HOW_IT_WORKS.md) - Simple explanation of all features

## Common Issues 🚨

1. **Bot Can't Access Channel?**
   - Make sure bot is in the channel
   - Check if channel ID is correct
   - Give bot permission to post

2. **Messages Not Forwarding?**
   - Check if bot is running
   - Verify channel IDs
   - Check bot permissions

3. **Edits Not Working?**
   - Make sure original message was sent by this bot
   - Check if message isn't too old

## Want More Details? 📚

Check out our guides:
1. [Complete Guide](docs/GUIDE.md) - Everything you need to know
2. [How It Works](docs/HOW_IT_WORKS.md) - Simple explanations