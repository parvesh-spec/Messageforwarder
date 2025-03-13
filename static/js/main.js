// Main JavaScript file for dashboard functionality
document.addEventListener('DOMContentLoaded', function() {
    // Initialize dropdown menus if any
    const dropdowns = document.querySelectorAll('.dropdown-toggle');
    dropdowns.forEach(dropdown => {
        dropdown.addEventListener('click', function() {
            const menu = this.nextElementSibling;
            menu.classList.toggle('show');
        });
    });

    // Initialize flash message auto-hide
    const flashMessages = document.querySelectorAll('.alert');
    flashMessages.forEach(message => {
        setTimeout(() => {
            message.style.opacity = '0';
            setTimeout(() => message.remove(), 300);
        }, 5000);
    });
});
