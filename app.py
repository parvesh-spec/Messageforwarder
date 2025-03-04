import os
import logging
import asyncio
from flask import Flask, render_template, request, redirect, url_for, jsonify, session
from flask_login import LoginManager, UserMixin, login_required, login_user, logout_user, current_user

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.urandom(24)

# Initialize Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Simple User class for Flask-Login
class User(UserMixin):
    def __init__(self, user_id):
        self.id = user_id

@login_manager.user_loader
def load_user(user_id):
    if 'is_authenticated' in session:
        return User(user_id)
    return None

# Routes
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login')
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    # Here we would typically get the list of channels from Telegram
    # For now, return a temporary list for testing
    channels = [
        {'id': '-1001234567890', 'name': 'Test Channel 1'},
        {'id': '-1001234567891', 'name': 'Test Channel 2'}
    ]
    return render_template('dashboard.html', channels=channels)

@app.route('/bot/toggle', methods=['POST'])
@login_required
def toggle_bot():
    try:
        status = request.form.get('status') == 'true'
        source = session.get('source_channel')
        destination = session.get('dest_channel')

        if not source or not destination:
            logger.error("Channel configuration missing")
            return jsonify({'error': 'Please configure source and destination channels first'}), 400

        # Format channel IDs
        if not source.startswith('-100'):
            source = f"-100{source.lstrip('-')}"
        if not destination.startswith('-100'):
            destination = f"-100{destination.lstrip('-')}"

        try:
            # Update main.py's channel configuration
            import main
            main.SOURCE_CHANNEL = source
            main.DESTINATION_CHANNEL = destination

            # Add debug logs
            logger.info(f"Setting channels in main.py - Source: {source}, Destination: {destination}")
            logger.info(f"Verifying main.py channels - Source: {main.SOURCE_CHANNEL}, Destination: {main.DESTINATION_CHANNEL}")

            # Restart the client to apply new configuration
            if status:
                logger.info("Attempting to restart Telegram client with new configuration...")
                asyncio.run_coroutine_threadsafe(main.restart_client(), main.loop)
                logger.info("Telegram client restart initiated")

            session['bot_running'] = status
            logger.info(f"Bot status changed to: {'running' if status else 'stopped'}")

            return jsonify({
                'status': status,
                'message': f"Bot is now {'running' if status else 'stopped'}"
            })
        except Exception as e:
            logger.error(f"Error configuring bot channels: {e}")
            return jsonify({'error': 'Failed to configure bot channels'}), 500

    except Exception as e:
        logger.error(f"Error toggling bot: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/update-channels', methods=['POST'])
@login_required
def update_channels():
    try:
        source = request.form.get('source')
        destination = request.form.get('destination')

        if not source or not destination:
            return jsonify({'error': 'Both source and destination channels are required'}), 400

        if source == destination:
            return jsonify({'error': 'Source and destination channels must be different'}), 400

        # Format channel IDs properly
        if not source.startswith('-100'):
            source = f"-100{source.lstrip('-')}"
        if not destination.startswith('-100'):
            destination = f"-100{destination.lstrip('-')}"

        # Store channel IDs in session
        session['source_channel'] = source
        session['dest_channel'] = destination
        logger.info(f"Updated channel configuration - Source: {source}, Destination: {destination}")

        # Update main.py's channel IDs
        import main
        main.SOURCE_CHANNEL = source
        main.DESTINATION_CHANNEL = destination
        logger.info(f"Updated main.py channel IDs - Source: {main.SOURCE_CHANNEL}, Destination: {main.DESTINATION_CHANNEL}")

        return jsonify({'message': 'Channel configuration updated successfully'})
    except Exception as e:
        logger.error(f"Error updating channels: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/add-replacement', methods=['POST'])
@login_required
def add_replacement():
    try:
        original = request.form.get('original')
        replacement = request.form.get('replacement')

        if not original or not replacement:
            return jsonify({'error': 'Both original and replacement text are required'}), 400

        # Update main.py's replacements
        import main
        main.TEXT_REPLACEMENTS[original] = replacement
        logger.info(f"Added text replacement: '{original}' → '{replacement}'")

        return jsonify({'message': 'Replacement added successfully'})
    except Exception as e:
        logger.error(f"Error adding replacement: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/get-replacements')
@login_required
def get_replacements():
    try:
        import main
        return jsonify(main.TEXT_REPLACEMENTS)
    except Exception as e:
        logger.error(f"Error getting replacements: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/remove-replacement', methods=['POST'])
@login_required
def remove_replacement():
    try:
        original = request.form.get('original')
        if not original:
            return jsonify({'error': 'Original text is required'}), 400

        import main
        if original in main.TEXT_REPLACEMENTS:
            del main.TEXT_REPLACEMENTS[original]
            logger.info(f"Removed text replacement for '{original}'")
            return jsonify({'message': 'Replacement removed successfully'})
        else:
            return jsonify({'error': 'Replacement not found'}), 404
    except Exception as e:
        logger.error(f"Error removing replacement: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/clear-replacements', methods=['POST'])
@login_required
def clear_replacements():
    try:
        import main
        main.TEXT_REPLACEMENTS.clear()
        logger.info("Cleared all text replacements")
        return jsonify({'message': 'All replacements cleared'})
    except Exception as e:
        logger.error(f"Error clearing replacements: {str(e)}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)