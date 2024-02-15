import json
import logging
import time

import telegram
import traceback
import textwrap
import re

import RankedChoice

from database import *
from load_config import *
from BaseLoader import BaseLoader
from result import Ok, Err, Result
from RankedVote import RankedVote
from MessageBuilder import MessageBuilder
from requests.models import PreparedRequest
from RankedChoice import SpecialVotes
from typing import List, Tuple

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update,
    WebAppInfo, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    CommandHandler, ApplicationBuilder, ContextTypes, CallbackContext,
    MessageHandler, filters
)

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logger = logging.getLogger(__name__)

"""
to do:
/ create poll
/ view poll options / votes
/ vote on a poll
/ fetch poll results 
automatically calculate + broadcast poll results
"""


def error_logger(update, context):
    """Log Errors caused by Updates."""
    logger.warning(
        'Update "%s" caused error "%s"',
        update, context.error
    )


def track_errors(func):
    def caller(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            print(traceback.format_exc())
            raise e

    return caller


class RankedChoiceBot(BaseLoader):
    def __init__(self, config_path='config.yml'):
        self.config_path = config_path
        self.bot = None
        self.app = None

        self.poll_max_options = 20
        self.poll_option_max_length = 100
        self.webhook_url = None

    def start_bot(self):
        self.bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
        self.webhook_url = TELE_CONFIG['webhook_url']
        self.app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        self.app.add_handler(MessageHandler(
            filters.StatusUpdate.WEB_APP_DATA, self.web_app_data
        ))

        # on different commands - answer in Telegram
        self.register_commands(self.app, commands_mapping=self.kwargify(
            start=self.start_handler,
            user_details=self.name_id_handler,
            create_poll=self.create_poll,
            view_poll=self.view_poll,
            vote=self.vote_for_poll,
            poll_results=self.fetch_poll_results,
            has_voted=self.has_voted,
            close_poll=self.close_poll,
            view_votes=self.view_votes,
            view_voters=self.view_poll_voters,
            help=self.show_help,

            vote_admin=self.vote_for_poll_admin,
            unclose_poll_admin=self.unclose_poll_admin,
            close_poll_admin=self.close_poll_admin
        ))

        # log all errors
        # dp.add_error_handler(error_logger)
        self.app.run_polling(allowed_updates=[
            Update.MESSAGE, Update.INLINE_QUERY, Update.CALLBACK_QUERY
        ])

    """
    @staticmethod
    @track_errors
    # Handle incoming WebAppData
    async def web_app_data(
        update: Update, context: CallbackContext.DEFAULT_TYPE
    ) -> None:
        # Print the received data and remove the button.
        # Here we use `json.loads`, since the WebApp sends 
        # the data JSON serialized string
        # (see webappbot.html)
        data = json.loads(update.effective_message.web_app_data.data)
        await update.message.reply_text()
    """

    @track_errors
    async def start_handler(
        self, update, context: ContextTypes.DEFAULT_TYPE
    ):
        # Send a message when the command /start is issued.
        message = update.message
        chat_type = update.message.chat.type
        args = context.args
        # print('CONTEXT_ARGS', args)

        if len(args) == 0:
            await update.message.reply_text('Bot started')
            return True
        if chat_type != 'private':
            await update.message.reply_text('Can only vote with /start in DM')
            return False

        pattern_match = re.match('poll_id=([0-9]+)', args[0])

        if not pattern_match:
            await update.message.reply_text(f'Invalid params: {args}')
            return False

        poll_id = int(pattern_match.group(1))
        user = update.message.from_user
        chat_username = user['username']

        view_poll_result = self._view_poll(
            poll_id=poll_id, chat_username=chat_username,
            bot_username=context.bot.username
        )

        if view_poll_result.is_ok():
            poll_message = view_poll_result.ok()
        else:
            error_message = view_poll_result.err()
            await error_message.call(message.reply_text)
            return False

        markup_layout = self.create_vote_markup(poll_id=poll_id)
        reply_markup = InlineKeyboardMarkup(markup_layout)
        await message.reply_text(poll_message, reply_markup=reply_markup)

    @track_errors
    async def web_app_data(self, update: Update, context: CallbackContext):
        data = json.loads(update.message.web_app_data.data)
        logger.info(f'WEB_APP_DATA = {data}')
        await update.message.reply_text(f"Your data was: {data}")

    def generate_poll_url(self, poll_id: int) -> str:
        req = PreparedRequest()
        params = {'poll_id': str(poll_id)}
        req.prepare_url(self.webhook_url, params)
        return req.url

    def create_vote_markup(
        self, poll_id: int
    ) -> List[List[InlineKeyboardButton]]:
        poll_url = self.generate_poll_url(poll_id=poll_id)
        logger.info(f'POLL_URL = {poll_url}')
        # create vote button for reply message
        markup_layout = [[InlineKeyboardButton(
            text='Vote', web_app=WebAppInfo(url=poll_url)
        )]]

        return markup_layout

    @track_errors
    async def name_id_handler(self, update, *args):
        """
        returns current user id and username
        """
        # when command /user_details is invoked
        user = update.message.from_user
        await update.message.reply_text(textwrap.dedent(f"""
            user id: {user['id']}
            username: {user['username']}
        """))

    @track_errors
    async def has_voted(self, update, *args, **kwargs):
        """
        usage:
        /has_voted {poll_id}
        """
        message = update.message
        user = update.message.from_user
        chat_username = user['username']

        poll_id = self.extract_poll_id(update)
        if poll_id is None:
            return False

        is_voter = self.is_poll_voter(
            poll_id=poll_id, chat_username=chat_username
        )

        if not is_voter:
            await message.reply_text(
                f"You're not a voter of poll {poll_id}"
            )
            return False

        has_voted = bool(Votes.select().join(
            PollVoters, on=(Votes.poll_voter_id == PollVoters.id)
        ).where(
            (Votes.poll_id == poll_id) &
            (PollVoters.username == chat_username)
        ).count())

        if has_voted:
            await message.reply_text("you've voted already")
        else:
            await message.reply_text("you haven't voted")

    @track_errors
    async def create_poll(self, update, context: ContextTypes.DEFAULT_TYPE):
        """
        example:
        ---------------------------
        /create_poll @asd @fad:
        what ice cream is the best
        mochi
        potato
        cookies and cream
        chocolate
        """
        creator_user = update.message.from_user
        creator_username = creator_user['username']
        message = update.message
        raw_text = message.text.strip()

        if ':' not in raw_text:
            await message.reply_text("poll creation format wrong")
            return False

        split_index = raw_text.index(':')
        # first part of command is all the users that are in the poll
        command_p1 = raw_text[:split_index].strip()
        # second part of command is the poll question + poll options
        command_p2 = raw_text[split_index + 1:].strip()

        lines = command_p2.split('\n')
        if len(lines) < 3:
            await message.reply_text('Poll requires at least 2 options')
            return False

        poll_question = lines[0].strip().replace('\n', '')
        poll_options = lines[1:]
        poll_options = [
            poll_option.strip().replace('\n', '')
            for poll_option in poll_options
        ]

        if len(poll_options) > self.poll_max_options:
            await message.reply_text(textwrap.dedent(f"""
                Poll can have at most {self.poll_max_options} options
                {len(poll_options)} poll options passed
            """))
            return False

        max_option_length = max([len(option) for option in poll_options])
        if max_option_length > self.poll_option_max_length:
            await message.reply_text(textwrap.dedent(f"""
                Poll option character limit is {self.poll_option_max_length}
                Longest option passed is {max_option_length} characters long
            """))
            return False

        # print('COMMAND_P2', lines)
        if ' ' in command_p1:
            command_p1 = command_p1[command_p1.index(' '):].strip()
        else:
            await message.reply_text('poll voters not specified!')

        poll_usernames = command_p1.split()
        poll_users = []

        for poll_user in poll_usernames:
            # TODO: find a way to get all users in a telegram group
            # telegram usernames must be at least 4 characters long
            if poll_user == 'all':
                if message.chat.type != 'group':
                    await message.reply_text(
                        'can only add all users in a group'
                    )
                    return False
                else:
                    await message.reply_text(
                        'adding all users in a group is not suppoerted'
                    )
                    return False
            else:
                if poll_user.startswith('@'):
                    poll_user = poll_user[1:]
                if len(poll_user) < 4:
                    await message.reply_text(
                        f'username too short: {poll_user}'
                    )
                    return False

                poll_users.append(poll_user)

        new_poll = Polls.create(
            desc=poll_question, creator=creator_username
        )

        new_poll.save()
        new_poll_id: int = new_poll.id
        assert isinstance(new_poll_id, int)
        poll_option_rows = []
        poll_user_rows = []

        for k, poll_option in enumerate(poll_options):
            poll_choice_number = k+1
            poll_option_rows.append(self.kwargify(
                poll_id=new_poll_id, option_name=poll_option,
                option_number=poll_choice_number
            ))

        for poll_user in poll_users:
            poll_user_rows.append(self.kwargify(
                poll_id=new_poll_id, username=poll_user
            ))

        group_id = update.message.chat_id
        chat = Chats.create(
            poll_id=new_poll_id, tele_id=group_id,
            broadcasted=False
        )

        with db.atomic():
            Options.insert_many(poll_option_rows).execute()
            PollVoters.insert_many(poll_user_rows).execute()
            chat.save()

        bot_username = context.bot.username
        poll_message = self.generate_poll_info(
            new_poll_id, poll_question, poll_options,
            bot_username=bot_username,
            num_voters=len(poll_users)
        )

        chat_type = update.message.chat.type
        reply_markup = None

        if chat_type == 'private':
            # create vote button for reply message
            markup_layout = self.create_vote_markup(poll_id=new_poll_id)
            reply_markup = InlineKeyboardMarkup(markup_layout)

        await message.reply_text(
            poll_message, reply_markup=reply_markup
        )

    @track_errors
    async def view_votes(self, update, *args, **kwargs):
        message = update.message
        extract_result = self.extract_poll_id(update)

        if extract_result.is_ok():
            poll_id = extract_result.ok()
        else:
            error_message = extract_result.err()
            await error_message.call(message.reply_text)
            return False

        user = update.message.from_user
        chat_username = user['username']
        # check if voter is part of the poll

        try:
            poll = Polls.select().where(Polls.id == poll_id).get()
        except Polls.DoesNotExist:
            await message.reply_text(f'poll {poll_id} does not exist')
            return False

        if not poll.closed:
            await message.reply_text(
                'poll votes can only be viewed after closing'
            )
            return False

        has_poll_access = self.has_poll_access(poll_id, chat_username)
        if not has_poll_access:
            await message.reply_text(
                f'You have no access to poll {poll_id}'
            )
            return False

        # get poll options in ascending order
        poll_option_rows = Options.select().where(
            Options.poll_id == poll_id
        ).order_by(Options.option_number)

        # map poll option ids to their option numbers
        # (option number is the position of the option in the poll)
        option_index_map = {}
        for poll_option_row in poll_option_rows:
            option_index_map[poll_option_row.id] = (
                poll_option_row.option_number
            )

        vote_rows = (Votes.select()
            .where(Votes.poll_id == poll_id)
            .order_by(Votes.option_id, Votes.ranking)
        )

        vote_sequence_map = {}
        for vote_row in vote_rows:
            """
            Maps voters to their ranked vote
            Each ranked vote is stored as a dictionary
            mapping their vote ranking to a vote_value
            Each vote_value is either a poll option_id 
            (which is always a positive number), 
            or either of the 0 or nil special votes
            (which are represented as negative numbers -1 and -2)
            """
            voter_id = vote_row.poll_voter_id

            if voter_id not in vote_sequence_map:
                vote_sequence_map[voter_id] = {}

            option_row = vote_row.option_id
            if option_row is None:
                vote_value = vote_row.special_value
                assert vote_value < 0
            else:
                vote_value = option_row.id
                assert vote_value > 0

            ranking_map = vote_sequence_map[voter_id]
            ranking_map[vote_row.ranking] = vote_value

        ranking_message = ''
        for voter_id in vote_sequence_map:
            # format vote sequence map into string rankings
            ranking_map = vote_sequence_map[voter_id]
            ranking_nos = sorted(ranking_map.keys())
            sorted_option_nos = [
                ranking_map[ranking] for ranking in ranking_nos
            ]

            # print('SORT-NOS', sorted_option_nos)
            str_rankings = []

            for vote_value in sorted_option_nos:
                if vote_value > 0:
                    str_rankings.append(str(vote_value))
                else:
                    str_rankings.append(
                        SpecialVotes(vote_value).to_string()
                    )

            rankings_str = ' > '.join(str_rankings).strip()
            ranking_message += rankings_str + '\n'

        ranking_message = ranking_message.strip()
        await message.reply_text(f'votes recorded:\n{ranking_message}')

    @track_errors
    async def unclose_poll_admin(self, update, *args, **kwargs):
        await self._set_poll_status(update, False)

    @track_errors
    async def close_poll_admin(self, update, *args, **kwargs):
        await self._set_poll_status(update, True)

    async def _set_poll_status(self, update, closed=True):
        message = update.message
        user = update.message.from_user
        user_id = user['id']

        if user_id != YAML_CONFIG['telegram']['sudo_id']:
            await message.reply_text('ACCESS DENIED')
            return False

        extract_result = self.extract_poll_id(update)

        if extract_result.is_ok():
            poll_id = extract_result.ok()
        else:
            error_message = extract_result.err()
            await error_message.call(message.reply_text)
            return False

        Polls.update({Polls.closed: closed}).where(
            Polls.id == poll_id
        ).execute()

        await message.reply_text(f'poll {poll_id} has been unclosed')

    @track_errors
    async def view_poll(self, update, context: ContextTypes.DEFAULT_TYPE):
        """
        example:
        /view_poll 3
        """
        message = update.message
        user = update.message.from_user
        chat_username = user['username']

        extract_result = self.extract_poll_id(update)

        if extract_result.is_ok():
            poll_id = extract_result.ok()
        else:
            error_message = extract_result.err()
            await error_message.call(message.reply_text)
            return False

        view_poll_result = self._view_poll(
            poll_id=poll_id, chat_username=chat_username,
            bot_username=context.bot.username
        )

        if view_poll_result.is_ok():
            poll_message = view_poll_result.ok()
            await message.reply_text(poll_message)
            return True
        else:
            error_message = view_poll_result.err()
            await error_message.call(message.reply_text)
            return False

    async def vote_and_report(self, raw_text, chat_username, message):
        vote_result = self._vote_for_poll(
            raw_text=raw_text, chat_username=chat_username,
            message=message
        )

        if vote_result.is_ok():
            poll_id = vote_result.ok()
        else:
            error_message = vote_result.err()
            await error_message.call(message.reply_text)
            return False

        winning_option_id = self.get_poll_winner(poll_id)

        # count number of eligible voters
        num_poll_voters = PollVoters.select().where(
            PollVoters.poll_id == poll_id
        ).count()
        # count number of people who voted
        num_poll_voted = self.fetch_voters(poll_id).count()
        everyone_voted = num_poll_voters == num_poll_voted

        if everyone_voted:
            if winning_option_id is not None:
                winning_options = Options.select().where(
                    Options.id == winning_option_id
                )

                option_name = winning_options[0].option_name
                await message.reply_text(textwrap.dedent(f"""
                    all members voted
                    poll winner is:
                    {option_name}
                """))
            else:
                await message.reply_text(textwrap.dedent(f"""
                    all members voted
                    poll has no winner
                """))
        else:
            await message.reply_text(textwrap.dedent(f"""
                vote has been registered
                vote count: {num_poll_voted}/{num_poll_voters} 
            """))

    @staticmethod
    def fetch_voters(poll_id):
        return PollVoters.select().join(
            Votes, on=(Votes.poll_voter_id == PollVoters.id)
        ).where(
            (Votes.poll_id == poll_id) &
            (Votes.ranking == 0)
        )

    @track_errors
    async def close_poll(self, update, *args, **kwargs):
        message = update.message
        extract_result = self.extract_poll_id(update)

        if extract_result.is_ok():
            poll_id = extract_result.ok()
        else:
            error_message = extract_result.err()
            await error_message.call(message.reply_text)
            return False

        user = message.from_user
        chat_username = user['username']

        try:
            poll = Polls.select().where(Polls.id == poll_id).get()
        except Polls.DoesNotExist:
            await message.reply_text(f'poll {poll_id} does not exist')
            return False

        if poll.creator != chat_username:
            await message.reply_text(
                'only poll creator is allowed to close poll'
            )
            return False

        Polls.update({Polls.closed: True}).where(
            Polls.id == poll.id
        ).execute()

        await message.reply_text('poll closed')

    @track_errors
    async def vote_for_poll_admin(self, update, *args, **kwargs):
        """
        telegram command formats:
        /vote_admin {username} {poll_id}: {option_1} > ... > {option_n}
        /vote_admin {username} {poll_id} {option_1} > ... > {option_n}
        examples:
        /vote 3: 1 > 2 > 3
        /vote 3 1 > 2 > 3
        """
        # vote for someone else
        message = update.message
        raw_text = message.text.strip()
        user = update.message.from_user
        user_id = user['id']

        if user_id != YAML_CONFIG['telegram']['sudo_id']:
            await message.reply_text('ACCESS DENIED')
            return False

        if ' ' not in raw_text:
            await message.reply_text('no user specified')
            return False

        raw_text = raw_text[raw_text.index(' ')+1:].strip()
        if ' ' not in raw_text:
            await message.reply_text('no poll_id specified (admin)')
            return False

        chat_username = raw_text[:raw_text.index(' ')].strip()
        # raw_text = raw_text[raw_text.index(' ')+1:].strip()
        # print('RAW', [raw_text])

        if chat_username.startswith('@'):
            chat_username = chat_username[1:]

        if ' ' not in raw_text:
            await message.reply_text('invalid format (admin)')
            return False

        print('CHAT_USERNAME', chat_username)
        # raw_text = raw_text[raw_text.index(' ')+1:].strip()
        # print('RAW', [raw_text])

        await self.vote_and_report(raw_text, chat_username, message)

    @track_errors
    async def vote_for_poll(self, update, *args, **kwargs):
        """
        telegram command formats
        /vote {poll_id}: {option_1} > {option_2} > ... > {option_n}
        /vote {poll_id} {option_1} > {option_2} > ... > {option_n}
        example:
        /vote 3: 1 > 2 > 3
        /vote 3 1 > 2 > 3
        """
        message = update.message
        raw_text = message.text.strip()
        user = update.message.from_user
        chat_username = user['username']

        await self.vote_and_report(raw_text, chat_username, message)

    def _vote_for_poll(
        self, raw_text, chat_username, message
    ) -> Result[int, MessageBuilder]:
        """
        telegram command format
        /vote {poll_id}: {option_1} > {option_2} > ... > {option_n}
        /vote {poll_id} {option_1} > {option_2} > ... > {option_n}
        example:
        /vote 3: 1 > 2 > 3
        /vote 3 1 > 2 > 3
        """
        error_message = MessageBuilder()
        print('RAW_VOTE_TEXT', [raw_text, chat_username])
        if ' ' not in raw_text:
            error_message.add('no poll id specified')
            return Err(error_message)

        unpack_result = self.unpack_rankings_and_poll_id(raw_text)

        if unpack_result.is_ok():
            poll_id, rankings = unpack_result.ok()
        else:
            assert isinstance(unpack_result, Err)
            return unpack_result

        # check if voter is part of the poll
        poll_voter = self.get_poll_voter(poll_id, chat_username)
        print('CC', poll_voter.count(), [chat_username, poll_id])

        if poll_voter.count() == 0:
            message.add(f"You're not a voter of poll {poll_id}")
            return Err(error_message)

        try:
            poll = Polls.select().where(Polls.id == poll_id).get()
        except Polls.DoesNotExist:
            error_message.add(f'Poll {poll_id} does not exist')
            return Err(error_message)

        if poll.closed:
            error_message.add('Poll has already been closed')
            return Err(error_message)

        poll_voter_id = poll_voter[0].id
        # print('POLL_VOTER_ID', poll_voter_id)

        vote_register_result = self.register_vote(
            poll_id, poll_voter_id=poll_voter_id,
            rankings=rankings
        )

        if vote_register_result.is_ok():
            vote_registered = vote_register_result.ok()
            if vote_registered:
                return Ok(poll_id)
            else:
                error_message.add('Vote registration failed')
                return Err(error_message)
        else:
            assert isinstance(vote_register_result, Err)
            return vote_register_result

    @staticmethod
    def parse_ranking(raw_ranking) -> int:
        raw_ranking = raw_ranking.strip()

        try:
            special_ranking = SpecialVotes.from_string(raw_ranking)
            assert special_ranking.value < 0
            return special_ranking.value
        except ValueError:
            ranking = int(raw_ranking)
            assert ranking > 0
            return ranking

    @classmethod
    def unpack_rankings_and_poll_id(
        cls, raw_text
    ) -> Result[Tuple[int, List[int]], MessageBuilder]:
        """
        raw_text format:
        {command} {poll_id}: {choice_1} > {choice_2} > ... > {choice_n}
        """
        error_message = MessageBuilder()
        # remove starting command from raw_text
        raw_arguments = raw_text[raw_text.index(' '):].strip()

        """
        catches input of format:
        {poll_id}: {choice_1} > {choice_2} > ... > {choice_n}
        {poll_id} {choice_1} > {choice_2} > ... > {choice_n}

        regex breakdown:
        ^ -> start of string
        ^[0-9]+:*\s+ -> poll_id, optional colon, and space 
        (\s*[1-9]+0*\s*>)* -> ranking number (>0) then arrow
        \s*[0-9]+ -> final ranking number
        $ -> end of string        
        """
        print('RAW', raw_arguments)
        pattern_match1 = re.match(
            '^[0-9]+:?\s+(\s*[1-9]+0*\s*>)*\s*([0-9]+|nil)$',
            raw_arguments
        )
        """
        catches input of format:
        {poll_id} {choice_1} {choice_2} ... {choice_n}

        regex breakdown:
        ^ -> start of string
        ([0-9]+):*\s* -> poll_id, optional colon
        ([1-9]+0*\s+)* -> ranking number (>0) then space
        ([0-9]+) -> final ranking number
        $ -> end of string        
        """
        pattern_match2 = re.match(
            '^([0-9]+):?\s*([1-9]+0*\s+)*([0-9]+|nil)$',
            raw_arguments
        )

        if pattern_match1:
            raw_arguments = raw_arguments.replace(':', '')
            seperator_index = raw_arguments.index(' ')
            raw_poll_id = int(raw_arguments[:seperator_index])
            raw_votes = raw_arguments[seperator_index:].strip()
            rankings = [
                cls.parse_ranking(ranking)
                for ranking in raw_votes.split('>')
            ]
        elif pattern_match2:
            raw_arguments = raw_arguments.replace(':', '')
            raw_arguments = re.sub('\s+', ' ', raw_arguments)
            raw_arguments_arr = raw_arguments.split(' ')
            raw_poll_id = int(raw_arguments_arr[0])
            raw_votes = raw_arguments_arr[1:]
            rankings = [
                cls.parse_ranking(ranking)
                for ranking in raw_votes
            ]
        else:
            error_message.add('input format is invalid')
            return Err(error_message)

        print('rankings =', rankings)
        if len(rankings) != len(set(rankings)):
            error_message.add('vote rankings must be unique')
            return Err(error_message)

        non_last_rankings = rankings[:-1]
        if (len(non_last_rankings) > 0) and (min(non_last_rankings) < 1):
            error_message.add(
                'vote rankings must be positive non-zero numbers'
            )
            return Err(error_message)

        try:
            poll_id = int(raw_poll_id)
        except ValueError:
            error_message.add(f'invalid poll id: {raw_arguments}')
            return Err(error_message)

        return Ok((poll_id, rankings))

    def register_vote(
        self, poll_id, poll_voter_id, rankings
    ) -> Result[bool, MessageBuilder]:
        """
        :param poll_id:
        :param poll_voter_id:
        :param rankings:
        :param message: telegram message object
        :return: true if vote was registered, false otherwise
        """
        error_message = MessageBuilder()
        poll_option_rows = Options.select().where(
            Options.poll_id == poll_id
        ).order_by(Options.option_number)

        poll_votes = []
        for ranking, choice in enumerate(rankings):
            poll_option_id, special_vote_val = None, None

            if choice > 0:
                try:
                    # specified vote choice is not in the list
                    # of available choices
                    poll_option_row = poll_option_rows[choice - 1]
                except IndexError:
                    error_message.add(f'invalid vote number: {choice}')
                    return Err(error_message)

                poll_option_id = poll_option_row.id
            else:
                # vote is a special value (0 or nil vote)
                # which gets translated to a negative integer here
                try:
                    SpecialVotes(choice)
                except ValueError:
                    error_message.add(f'invalid special vote: {choice}')
                    return Err(error_message)

                special_vote_val = choice

            poll_vote = self.kwargify(
                poll_id=poll_id, poll_voter_id=poll_voter_id,
                option_id=poll_option_id, special_value=special_vote_val,
                ranking=ranking
            )

            poll_votes.append(poll_vote)

        # clear previous vote by the same user on the same poll
        delete_vote_query = Votes.delete().where(
            (Votes.poll_voter_id == poll_voter_id) &
            (Votes.poll_id == poll_id)
        )

        with db.atomic():
            delete_vote_query.execute()
            Votes.insert_many(poll_votes).execute()

        return Ok(True)

    @staticmethod
    def get_poll_winner(poll_id):
        num_poll_voters = PollVoters.select().where(
            PollVoters.poll_id == poll_id
        ).count()

        # get votes for the poll sorted from
        # the low ranking option (most favored)
        # to the highest ranking option (least favored)
        votes = Votes.select().where(
            Votes.poll_id == poll_id
        ).order_by(Votes.ranking.asc())

        vote_map = {}
        for vote in votes:
            voter = vote.poll_voter_id
            if voter not in vote_map:
                vote_map[voter] = RankedVote()

            option_row = vote.option_id
            if option_row is None:
                vote_value = vote.special_value
            else:
                vote_value = option_row.id

            # print('VOTE_VAL', vote_value, int(vote_value))
            vote_map[voter].add_next_choice(vote_value)

        vote_flat_map = list(vote_map.values())
        print('FLAT_MAP', vote_flat_map)

        winning_option_id = RankedChoice.ranked_choice_vote(
            vote_flat_map, num_voters=num_poll_voters
        )
        return winning_option_id

    @track_errors
    async def show_help(self, update, *args, **kwargs):
        message = update.message
        await message.reply_text(textwrap.dedent("""
        /start - start bot
        /user_details - shows your username and user id
        ——————————————————
        /create_poll @user_1 @user_2 ... @user_n:
        poll title
        poll option 1
        poll option 2
        ...
        poll option m
        - creates a new poll
        ——————————————————
        /view_poll {poll_id} - shows poll details given poll_id
        ——————————————————
        /vote {poll_id}: {option_1} > {option_2} > ... > {option_n} 
        /vote {poll_id} {option_1} > {option_2} > ... > {option_n} 
        /vote {poll_id} {option_1} {option_2} ... {option_n} 

        Last option can also accept 2 special values, 0 and nil:
            > Vote 0 if you want to vote for none of the options in the poll
            > Vote nil if you want to remove yourself from the poll 

        - vote for the poll with the specified poll_id
        requires that the user is one of the registered 
        voters of the poll
        ——————————————————
        /poll_results {poll_id}
        - returns poll results if the poll has been closed
        ——————————————————
        /has_voted {poll_id} 
        - tells you if you've voted for the poll with the 
        specified poll_id
        ——————————————————
        /close_poll {poll_id}
        - close the poll with the specified poll_id
        note that only the poll's creator is allowed 
        to issue this command to close the poll
        ——————————————————
        /view_votes {poll_id}
        - view all the votes entered for the poll 
        with the specified poll_id. This can only be done
        after the poll has been closed first
        ——————————————————
        /view_voters {poll_id}
        - show which voters have voted and which have not
        ——————————————————
        /help - view commands available to the bot
        """))

    @track_errors
    async def view_poll_voters(self, update, *args, **kwargs):
        """
        /view_voters {poll_id}
        :param update: 
        :param args: 
        :param kwargs: 
        :return: 
        """
        message = update.message
        extract_result = self.extract_poll_id(update)

        if extract_result.is_ok():
            poll_id = extract_result.ok()
        else:
            error_message = extract_result.err()
            await error_message.call(message.reply_text)
            return False

        user = message.from_user
        chat_username = user['username']
        # check if voter is part of the poll

        has_poll_access = self.has_poll_access(poll_id, chat_username)
        if not has_poll_access:
            await message.reply_text(f'You have no access to poll {poll_id}')
            return False

        poll_voters_voted = self.fetch_voters(poll_id)
        poll_voters = PollVoters.select().where(
            PollVoters.poll_id == poll_id
        )

        voter_usernames = [
            voter.username for voter in poll_voters
        ]
        voted_usernames = [
            voter.username for voter in poll_voters_voted
        ]
        not_voted_usernames = list(
            set(voter_usernames) - set(voted_usernames)
        )

        await message.reply_text(textwrap.dedent(f"""
            voted:
            {' '.join(voted_usernames)}
            not voted:
            {' '.join(not_voted_usernames)}
        """))

    @track_errors
    async def fetch_poll_results(self, update, *args, **kwargs):
        """
        /poll_results 5
        :param update:
        :param args:
        :param kwargs:
        :return:
        """
        message = update.message
        extract_result = self.extract_poll_id(update)

        if extract_result.is_ok():
            poll_id = extract_result.ok()
        else:
            error_message = extract_result.err()
            await error_message.call(message.reply_text)
            return False

        user = update.message.from_user
        chat_username = user['username']
        # check if voter is part of the poll

        has_poll_access = self.has_poll_access(poll_id, chat_username)
        if not has_poll_access:
            await message.reply_text(f'You have no access to poll {poll_id}')
            return False

        winning_option_id = self.get_poll_winner(poll_id)

        if winning_option_id is None:
            await message.reply_text('no poll winner so far')
            return False
        else:
            winning_options = Options.select().where(
                Options.id == winning_option_id
            )

            option_name = winning_options[0].option_name
            await message.reply_text(f'poll winner is:\n{option_name}')

    @staticmethod
    def kwargify(**kwargs):
        return kwargs

    @staticmethod
    def register_commands(
        dispatcher, commands_mapping, wrap_func=lambda func: func
    ):
        for command_name in commands_mapping:
            handler = commands_mapping[command_name]
            wrapped_handler = wrap_func(handler)
            dispatcher.add_handler(CommandHandler(
                command_name, wrapped_handler
            ))


if __name__ == '__main__':
    rcv_bot = RankedChoiceBot()
    rcv_bot.start_bot()
