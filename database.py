import yaml
import datetime

from peewee import *

with open('config.yml', 'r') as config_file:
    yaml_data = yaml.safe_load(config_file)

username = yaml_data['database']['user']
password = yaml_data['database']['password']

db = MySQLDatabase(
    'ranked_choice_voting', user=username,
    password=password
)


class BaseModel(Model):
    class Meta:
        database = db


class Polls(BaseModel):
    id = PrimaryKeyField()
    desc = TextField(default="")
    close_time = TimestampField(default=None)
    open_time = TimestampField(default=datetime.datetime.now)
    closed = BooleanField(default=False)
    creator = CharField(max_length=255, default=None)


class Chats(BaseModel):
    id = PrimaryKeyField()
    poll_id = ForeignKeyField(Polls, to_field='id')
    tele_id = IntegerField()
    broadcasted = BooleanField(default=False)


class PollVoters(BaseModel):
    id = PrimaryKeyField()
    poll_id = ForeignKeyField(Polls, to_field='id')
    username = CharField(max_length=255)


class Options(BaseModel):
    id = PrimaryKeyField()
    poll_id = ForeignKeyField(Polls, to_field='id')
    option_name = CharField(max_length=255)


class Votes(BaseModel):
    id = PrimaryKeyField()
    poll_id = ForeignKeyField(Polls, to_field='id')
    poll_voter_id = ForeignKeyField(PollVoters, to_field='id')
    option_id = ForeignKeyField(Options, to_field='id')
    ranking = IntegerField()


# Create tables (if they don't exist)
db.connect()
db.create_tables([
    Polls, Chats, PollVoters, Options, Votes
], safe=True)

