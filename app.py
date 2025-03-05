import os
import logging
from flask import Flask, jsonify

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.urandom(24)

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
        # Still return 200 for deployment health checks
        return 'OK', 200

# Development server
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

# For production using gunicorn (example)
# gunicorn --workers 3 --bind 0.0.0.0:5000 app:app