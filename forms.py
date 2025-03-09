from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField
from wtforms.validators import DataRequired, Email, Length, EqualTo, ValidationError
from email_validator import validate_email, EmailNotValidError

class LoginForm(FlaskForm):
    class Meta:
        csrf = True  # Enable CSRF protection for this form

    email = StringField('Email', validators=[
        DataRequired(message="Email is required"),
        Email(message='Please enter a valid email address')
    ])
    password = PasswordField('Password', validators=[
        DataRequired(message="Password is required")
    ])

class RegisterForm(FlaskForm):
    class Meta:
        csrf = True  # Enable CSRF protection for this form

    email = StringField('Email', validators=[
        DataRequired(message="Email is required"),
        Email(message='Please enter a valid email address')
    ])
    password = PasswordField('Password', validators=[
        DataRequired(message="Password is required"),
        Length(min=8, message='Password must be at least 8 characters long')
    ])
    confirm_password = PasswordField('Confirm Password', validators=[
        DataRequired(message="Please confirm your password"),
        EqualTo('password', message='Passwords must match')
    ])

    def validate_email(self, field):
        try:
            validate_email(field.data)
        except EmailNotValidError as e:
            raise ValidationError(str(e))