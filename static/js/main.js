// Main JavaScript file for common functionality

// Show flash messages
function showFlashMessage(message, type = 'info') {
    const flashContainer = document.getElementById('flash-messages');
    if (!flashContainer) return;

    const messageDiv = document.createElement('div');
    messageDiv.className = `alert alert-${type}`;
    messageDiv.textContent = message;

    flashContainer.appendChild(messageDiv);

    // Auto-hide after 5 seconds
    setTimeout(() => {
        messageDiv.remove();
    }, 5000);
}

// Handle form submissions with CSRF token
function setupFormSubmission() {
    const forms = document.querySelectorAll('form');
    forms.forEach(form => {
        form.addEventListener('submit', function(e) {
            const csrfToken = document.querySelector('input[name="csrf_token"]');
            if (csrfToken) {
                // Add CSRF token to headers for fetch requests
                const headers = new Headers({
                    'X-CSRFToken': csrfToken.value,
                    'Content-Type': 'application/x-www-form-urlencoded'
                });
            }
        });
    });
}

// Initialize when DOM is loaded
document.addEventListener('DOMContentLoaded', function() {
    setupFormSubmission();
});
