import os
import logging
from flask import Flask, render_template, session, jsonify
from flask_login import LoginManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.urandom(24)

# Initialize login manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@app.route('/')
def health_check():
    """Basic health check endpoint"""
    try:
        return jsonify({
            'status': 'healthy',
            'message': 'Service is running'
        }), 200
    except Exception as e:
        logger.error(f"Health check error: {str(e)}")
        return 'OK', 200  # Return 200 for deployment health checks

@app.route('/login')
def login():
    return render_template('login.html')

@app.errorhandler(404)
def not_found_error(error):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('500.html'), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)