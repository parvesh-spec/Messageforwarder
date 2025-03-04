from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

db = SQLAlchemy(model_class=Base)

class User(db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), unique=True, nullable=False)
    telegram_id = db.Column(db.BigInteger, unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    sessions = db.relationship('UserSession', backref='user', lazy=True)
    channels = db.relationship('Channel', backref='user', lazy=True)
    bot_config = db.relationship('BotConfig', backref='user', lazy=True)

class UserSession(db.Model):
    __tablename__ = 'user_sessions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    session_id = db.Column(db.String(100), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, default=True)

class Channel(db.Model):
    __tablename__ = 'channels'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    telegram_channel_id = db.Column(db.BigInteger, nullable=False)
    channel_name = db.Column(db.String(100))
    is_source = db.Column(db.Boolean, default=False)
    is_destination = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)

class BotConfig(db.Model):
    __tablename__ = 'bot_configs'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    is_active = db.Column(db.Boolean, default=False)
    text_replacement_enabled = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, onupdate=datetime.utcnow)