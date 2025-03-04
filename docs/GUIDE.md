# Telegram Auto-Forwarding Bot - Complete Guide

## 1. What This Bot Can Do

### Main Features
- Forward messages from one channel to another
- Send messages as new (not as forwards)
- Replace specific words in messages
- Sync message edits between channels
- Handle all types of media (photos, videos, files)

### Example Use Cases
1. **Message Forwarding**
   ```
   Channel A (source) -> Bot -> Channel B (destination)
   ```

2. **Word Replacement**
   ```
   Original: "Hello everyone!"
   After replacement: "Hi everyone!"
   ```

3. **Edit Syncing**
   ```
   Channel A: Edit "Meeting at 3 PM" to "Meeting at 4 PM"
   Channel B: Automatically updates to "Meeting at 4 PM"
   ```

## 2. Setting Up the Bot

### Step-by-Step Setup
1. **Get Telegram API Details**
   - Visit: https://my.telegram.org
   - Log in with your phone number
   - Go to 'API development tools'
   - Create a new application
   - Save your API_ID and API_HASH

2. **Install Required Software**
   ```bash
   pip install telethon
   ```

3. **Run the Bot**
   ```bash
   python main.py
   ```

4. **First-Time Login**
   - Enter your phone number (with country code)
   - Enter verification code from Telegram
   - If you have two-factor auth, enter your password

### Channel Setup
1. **Source Channel**
   - For public channels: Use username (e.g., @channelname)
   - For private channels: Use ID with -100 prefix (e.g., -1001234567890)

2. **Destination Channel**
   - Same format as source channel
   - Make sure bot is a member with posting rights

## 3. Using the Bot

### Basic Commands
```
/status             - Check current settings
/help               - Show all commands
/replace old|new    - Add word replacement
/replacements       - List all replacements
/clearreplacements  - Remove all replacements
```

### Examples

1. **Adding Word Replacement**
   ```
   You: /replace Hello|Hi
   Bot: ✓ Added replacement: 'Hello' → 'Hi'
   ```

2. **Checking Status**
   ```
   You: /status
   Bot: Bot is running
       Forwarding from: @sourceChannel
       To: @destinationChannel
       Active replacements: 2
   ```

## 4. Troubleshooting

### Common Issues and Solutions

1. **Can't Access Channel**
   - Check if bot is member of both channels
   - Verify channel IDs are correct
   - Ensure bot has posting permissions

2. **Messages Not Forwarding**
   - Confirm bot is running
   - Check channel ID format
   - Verify bot permissions

3. **Edits Not Syncing**
   - Message might be too old
   - Check if original message was forwarded by this bot

### Error Messages Explained

1. "Cannot find any entity"
   - Bot can't access the channel
   - Solution: Add bot to channel or check ID

2. "Not enough rights"
   - Bot lacks required permissions
   - Solution: Make bot admin or give posting rights

3. "Flood wait"
   - Too many requests
   - Solution: Wait for specified time

## 5. Tips and Best Practices

1. **Channel Management**
   - Keep bot as admin in both channels
   - Regular status checks using `/status`
   - Clear unused replacements periodically

2. **Word Replacements**
   - Keep replacement list manageable
   - Test replacements with sample messages
   - Use `/clearreplacements` to start fresh

3. **Performance**
   - Bot processes messages instantly
   - Edits sync in real-time
   - Media forwarding might take longer

Need more help? Check error messages in bot logs or contact support.
