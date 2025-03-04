# How Does This Bot Work? 🤖

Let's understand each part of the bot in simple terms!

## 1. Message Forwarding 📬

Imagine the bot is like a postal worker:
```
You ➡️ Source Channel ➡️ Bot ➡️ Destination Channel
```

What happens:
1. Someone sends a message in the first channel
2. Bot sees it immediately
3. Bot takes that message
4. Bot sends it to the second channel
5. Message appears as new (not as a forward)

## 2. Word Replacement 📝

The bot can change certain words automatically:

Example:
```
Original Message: "Hello everyone, meeting tomorrow"
You told bot: Change "Hello" to "Hi"
Final Message: "Hi everyone, meeting tomorrow"
```

How to set up replacements:
1. Send this to bot: `/replace Hello|Hi`
2. Bot will now change "Hello" to "Hi" in all messages
3. You can add many word replacements

## 3. Message Editing ✏️

When you edit a message, the bot updates it everywhere:

```
Step 1: Original Message
Channel 1: "Meeting at 3 PM"
Channel 2: "Meeting at 3 PM"

Step 2: You Edit Message
Channel 1: "Meeting at 4 PM" (you changed this)
Channel 2: "Meeting at 4 PM" (bot changed this automatically)
```

How it works:
1. Bot remembers which messages are connected
2. When you edit in first channel
3. Bot finds the same message in second channel
4. Bot makes the same change there

## 4. Bot Commands 🎮

You can control the bot by sending these messages:

1. `/status`
   - Shows if bot is working
   - Shows which channels it's connecting
   - Shows what words it's replacing

2. `/replace word1|word2`
   - Tells bot to replace "word1" with "word2"
   - Example: `/replace Hello|Hi`

3. `/replacements`
   - Shows all word replacements
   - Helps you remember what you set up

4. `/clearreplacements`
   - Removes all word replacements
   - Starts fresh

5. `/help`
   - Shows all commands you can use
   - Explains how to use them

## Common Problems and Solutions 🔧

1. Messages not showing up?
   - Check if bot is in both channels
   - Make sure bot can send messages
   - Check if bot is running

2. Edits not working?
   - Message must be sent through this bot
   - Can't edit very old messages
   - Bot must be admin in both channels

3. Word replacement not working?
   - Check spelling exactly
   - Use `/replacements` to see current settings
   - Try clearing and setting up again

Need more help? Just ask! 😊
