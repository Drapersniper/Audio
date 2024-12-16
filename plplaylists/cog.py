from __future__ import annotations

import collections
import contextlib
import itertools
import json
import typing
from pathlib import Path
from typing import Literal

import discord
from dacite import from_dict
from discord import app_commands
from redbot.core import Config, commands
from redbot.core.i18n import Translator, cog_i18n
from redbot.core.utils.chat_formatting import bold, box, humanize_list
from tabulate import tabulate

from pylav.constants.playlists import BUNDLED_PLAYLIST_IDS
from pylav.core.client import Client
from pylav.core.context import PyLavContext
from pylav.exceptions.client import PyLavInvalidArgumentsException
from pylav.exceptions.playlist import InvalidPlaylistException
from pylav.extension.red.ui.menus.generic import PaginatingMenu
from pylav.extension.red.ui.menus.playlist import PlaylistCreationFlow, PlaylistManageFlow
from pylav.extension.red.ui.prompts.playlists import maybe_prompt_for_playlist
from pylav.extension.red.ui.sources.playlist import PlaylistListSource, TrackMappingSource
from pylav.extension.red.utils import CompositeMetaClass, rgetattr
from pylav.extension.red.utils.decorators import always_hidden, invoker_is_dj, requires_player
from pylav.helpers.discord.converters.playlists import PlaylistConverter
from pylav.helpers.discord.converters.queries import QueryPlaylistConverter
from pylav.helpers.format.ascii import EightBitANSI
from pylav.helpers.format.strings import shorten_string
from pylav.nodes.api.responses.track import Track as Track_namespace_conflict
from pylav.players.query.obj import Query
from pylav.players.tracks.obj import Track
from pylav.storage.models.playlist import Playlist
from pylav.type_hints.bot import DISCORD_BOT_TYPE, DISCORD_COG_TYPE_MIXIN, DISCORD_INTERACTION_TYPE

_ = Translator("PyLavPlaylists", Path(__file__))


@cog_i18n(_)
class PyLavPlaylists(
    DISCORD_COG_TYPE_MIXIN,
    metaclass=CompositeMetaClass,
):
    """PyLav playlist management commands"""

    lavalink: Client

    __version__ = "1.0.0"

    slash_playlist = app_commands.Group(
        name="playlist",
        description=shorten_string(max_length=100, string=_("Control PyLav playlists")),
        extras={"red_force_enable": True},
    )

    def __init__(self, bot: DISCORD_BOT_TYPE, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bot = bot
        self._config = Config.get_conf(self, identifier=208903205982044161)

    async def red_delete_data_for_user(
        self,
        *,
        requester: Literal["discord_deleted_user", "owner", "user", "user_strict"],
        user_id: int,
    ) -> None:
        """
        Method for finding users data inside the cog and deleting it.
        """
        await self._config.user_from_id(user_id).clear()

    @slash_playlist.command(name="version")
    @app_commands.guild_only()
    async def slash_playlist_version(self, interaction: DISCORD_INTERACTION_TYPE) -> None:
        """Show the version of the Cog and PyLav"""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await self.bot.get_context(interaction)
        data = [
            (EightBitANSI.paint_white(self.__class__.__name__), EightBitANSI.paint_blue(self.__version__)),
            (EightBitANSI.paint_white("PyLav"), EightBitANSI.paint_blue(context.pylav.lib_version)),
        ]

        await context.send(
            embed=await context.pylav.construct_embed(
                description=box(
                    tabulate(
                        data,
                        headers=(
                            EightBitANSI.paint_yellow(_("Library / Cog"), bold=True, underline=True),
                            EightBitANSI.paint_yellow(_("Version"), bold=True, underline=True),
                        ),
                        tablefmt="fancy_grid",
                    ),
                    lang="ansi",
                ),
                messageable=context,
            ),
            ephemeral=True,
        )

    @slash_playlist.command(name="create", description=shorten_string(max_length=100, string=_("Create a playlist")))
    @app_commands.describe(
        url=shorten_string(
            max_length=100,
            string=_(
                "The URL of the playlist to create. YouTube, Spotify, SoundCloud, Apple Music playlists or albums"
            ),
        ),
        name=shorten_string(max_length=100, string=_("The name of the playlist")),
    )
    @app_commands.guild_only()
    async def slash_playlist_create(
        self, interaction: DISCORD_INTERACTION_TYPE, url: QueryPlaylistConverter = None, *, name: str = None
    ):  # sourcery skip: low-code-quality
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await self.bot.get_context(interaction)
        add_queue = False
        timed_out = False
        if not (name or url):
            playlist_prompt = PlaylistCreationFlow(
                cog=self,
                original_author=context.interaction.user if context.interaction else context.author,
                timeout=120,
            )

            title = _("Let us create a playlist!")
            description = _(
                "(**1**) - Apply changes to playlist.\n"
                "(**2**) - Cancel any changes made.\n"
                "(**3**) - Add a name to the playlist.\n"
                "(**4**) - Link this playlist to an existing playlist/album.\n"
                "(**5**) - Add all tracks from the queue to the playlist.\n\n"
                "If you want the playlist name to be as the original playlist simply set the URL but no name.\n\n"
            )
            await playlist_prompt.start(ctx=context, title=title, description=description)
            timed_out = await playlist_prompt.wait()

            url = playlist_prompt.url
            name = playlist_prompt.name
            add_queue = playlist_prompt.queue
            if url:
                add_queue = False
                url = await Query.from_string(url)
        name = name or f"{context.message.id}"
        if url:
            tracks_response = await context.pylav.get_tracks(url, player=context.player)
            match tracks_response.loadType:
                case "track":
                    tracks = [tracks_response.data]
                    artwork = tracks_response.data.pluginInfo.artworkUrl
                case "search":
                    tracks = tracks_response.data
                    artwork = None
                case "playlist":
                    tracks = tracks_response.data.tracks
                    artwork = tracks_response.data.pluginInfo.artworkUrl
                    name = name or tracks_response.data.info.name
                case __:
                    artwork = None
                    tracks = []
                    name = name or f"{context.message.id}"
            url = url.query_identifier
        else:
            artwork = None
            if add_queue and context.player:
                tracks = context.player.queue.raw_queue
                tracks = [track._processed for track in tracks if track.encoded] if tracks else []
            else:
                tracks = []
            url = None
        if not tracks and timed_out:
            await context.send(
                embed=await context.pylav.construct_embed(
                    title=_("I did not create this playlist."),
                    description=_("No tracks were provided in time."),
                    messageable=context,
                ),
                ephemeral=True,
            )
            return
        if name is None:
            name = f"{context.message.id}"
        await context.pylav.playlist_db_manager.create_or_update_user_playlist(
            identifier=context.message.id, author=context.author.id, name=name, url=url, tracks=tracks
        )
        await context.send(
            embed=await context.pylav.construct_embed(
                title=_("I have created a new playlist."),
                description=_(
                    "Name: `{name_variable_do_not_translate}`\nIdentifier: `{id_variable_do_not_translate}`\nTracks: `{track_count_variable_do_not_translate}`"
                ).format(
                    name_variable_do_not_translate=name,
                    id_variable_do_not_translate=context.message.id,
                    track_count_variable_do_not_translate=len(tracks),
                ),
                messageable=context,
                thumbnail=artwork,
            ),
            ephemeral=True,
        )

    @slash_playlist.command(name="list", description=_("List all playlists you have access to on the invoked context"))
    @app_commands.guild_only()
    async def slash_playlist_list(self, interaction: DISCORD_INTERACTION_TYPE):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await self.bot.get_context(interaction)
        playlists = await context.pylav.playlist_db_manager.get_all_for_user(
            requester=context.author.id,
            vc=rgetattr(context.author, "voice.channel", None),
            guild=context.guild,
            channel=context.channel,
        )
        playlists = list(itertools.chain.from_iterable(playlists))
        if not playlists:
            await context.send(
                embed=await context.pylav.construct_embed(description=_("You have no playlists"), messageable=context),
                ephemeral=True,
            )
            return
        await PaginatingMenu(
            cog=self,
            bot=self.bot,
            source=PlaylistListSource(cog=self, pages=playlists),
            delete_after_timeout=True,
            timeout=120,
            original_author=context.interaction.user if context.interaction else context.author,
        ).start(context)

    @slash_playlist.command(
        name="manage",
        description=_("Manage a playlist"),
    )
    @app_commands.describe(
        operation=_("The operation to perform on the playlist, if not specified a menu will be shown for full control"),
        playlist=_("The playlist to perform the operation on"),
    )
    @app_commands.guild_only()
    async def slash_playlist_manage(
        self,
        interaction: DISCORD_INTERACTION_TYPE,
        playlist: PlaylistConverter,
        operation: Literal["Info", "Save", "Play", "Delete"] = None,
    ):  # sourcery skip: low-code-quality
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await self.bot.get_context(interaction)

        # Could shortcut any other alias - but the issue comes with clarity and confirmation.
        invoked_with_start = operation == "Play"
        invoked_with_delete = operation == "Delete"
        invoked_with_queue = operation == "Save"
        invoked_with_info = operation == "Info"

        if invoked_with_queue and not context.player.queue.size():
            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context,
                    title=_("Nothing to save"),
                    description=_("There is nothing in the queue to save."),
                ),
                ephemeral=True,
            )
            return

        playlists: list[Playlist] = playlist
        playlist = await maybe_prompt_for_playlist(cog=self, playlists=playlists, context=context)
        if not playlist:
            return
        if invoked_with_start:
            await self.command_playlist_play.callback(self, context, playlist=[playlist], reverse=False)  # type: ignore
            return
        if invoked_with_info:
            await PaginatingMenu(
                bot=self.bot,
                cog=self,
                source=TrackMappingSource(
                    guild_id=context.guild.id,
                    cog=self,
                    author=context.author,
                    entries=await playlist.fetch_tracks(),
                    playlist=playlist,
                ),
                delete_after_timeout=True,
                starting_page=0,
                original_author=context.author,
            ).start(context)
            return
        if playlist.id not in BUNDLED_PLAYLIST_IDS:
            manageable = await playlist.can_manage(bot=self.bot, requester=context.author)
        else:
            manageable = False
        if invoked_with_delete and not manageable:
            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context,
                    description=_(
                        "{user_variable_do_not_translate}, playlist {playlist_name_variable_do_not_translate} cannot be managed by yourself."
                    ).format(
                        user_variable_do_not_translate=context.author.mention,
                        playlist_name_variable_do_not_translate=await playlist.get_name_formatted(with_url=True),
                    ),
                ),
                ephemeral=True,
            )
            return

        if manageable:
            info_description = _(
                "(**1**){space_variable_do_not_translate} - Apply changes to playlist.\n"
                "(**2**){space_variable_do_not_translate} - Cancel any changes made and close the menu.\n"
                "(**3**){space_variable_do_not_translate} - Delete this playlist.\n"
                "(**4**){space_variable_do_not_translate} - Remove all tracks from this playlist.\n"
                "(**5**){space_variable_do_not_translate} - Update the playlist with the latest tracks.\n"
                "Please note that this action will ignore any tracks added/removed via this menu.\n"
                "(**6**){space_variable_do_not_translate} - Change the name of the playlist.\n"
                "(**7**){space_variable_do_not_translate} - Link this playlist to an existing playlist/album.\n"
                "(**8**){space_variable_do_not_translate} - Add a query to this playlist.\n"
                "(**9**){space_variable_do_not_translate} - Remove a query from this playlist.\n"
                "(**10**) - Download the playlist file.\n"
                "(**11**) - Add current playlist to the queue.\n"
                "(**12**) - Show tracks in current playlist.\n"
                "(**13**) - Add tracks from queue to this playlist.\n"
                "(**14**) - Remove duplicate entries in the playlist.\n\n"
                "The add/remove track buttons can be used multiple times to "
                "add/remove multiple tracks and playlists at once.\n\n"
                "A query is anything playable by the play command - any query can be used by the add/remove buttons\n\n"
                "The clear button will always be run first before any other operations.\n"
                "The URL button will always run last - "
                "Linking a playlist via the URL will overwrite any tracks added or removed to this playlist.\n\n"
                "If you interact with a button multiple times other than the add/remove buttons "
                "only the last interaction will take effect.\n\n\n"
            )
        else:
            info_description = _(
                "(**1**) - Close the menu.\n"
                "(**2**) - Update the playlist with the latest tracks.\n"
                "(**3**) - Download the playlist file.\n"
                "(**4**) - Add current playlist to the queue.\n"
                "(**5**) - Show tracks in current playlist.\n\n\n"
            )

        playlist_info = _(
            "__**Currently managing**__:\n"
            "**Name**:{space_variable_do_not_translate}{space_variable_do_not_translate}{space_variable_do_not_translate}{playlist_name_variable_do_not_translate}\n"
            "**Scope**:{space_variable_do_not_translate}{space_variable_do_not_translate}{space_variable_do_not_translate}{scope_variable_do_not_translate}\n"
            "**Author**:{space_variable_do_not_translate}{space_variable_do_not_translate}{author_variable_do_not_translate}\n"
            "**Tracks**:{space_variable_do_not_translate}{space_variable_do_not_translate}{space_variable_do_not_translate}{tracks_variable_do_not_translate} tracks\n"
            "**URL**:{space_variable_do_not_translate}{space_variable_do_not_translate}{space_variable_do_not_translate}{space_variable_do_not_translate}{space_variable_do_not_translate}{url_variable_do_not_translate}\n"
        )
        playlist_prompt = PlaylistManageFlow(
            cog=self,
            original_author=context.author,
            timeout=600,
            playlist=playlist,
            manageable=manageable,
        )
        if not (invoked_with_delete or invoked_with_queue):
            name = await playlist.fetch_name()
            if manageable:
                title = _("Let us manage: {playlist_name_variable_do_not_translate}.").format(
                    playlist_name_variable_do_not_translate=name
                )
            else:
                title = _("Let us take a look at: {playlist_name_variable_do_not_translate}.").format(
                    playlist_name_variable_do_not_translate=name
                )
            description = info_description + playlist_info

            description = description.format(
                playlist_name_variable_do_not_translate=await playlist.get_name_formatted(with_url=True),
                scope_variable_do_not_translate=await playlist.get_scope_name(
                    bot=self.bot, mention=True, guild=context.guild
                ),
                author_variable_do_not_translate=await playlist.get_author_name(bot=self.bot, mention=True),
                url_variable_do_not_translate=await playlist.fetch_url() or _("N/A"),
                tracks_variable_do_not_translate=await playlist.size(),
                space_variable_do_not_translate="\N{EN SPACE}",
            )

            await playlist_prompt.start(ctx=context, title=title, description=description)
            await playlist_prompt.completed.wait()

        if manageable and invoked_with_delete:
            playlist_prompt.delete = True
            playlist_prompt.cancelled = False

        if manageable and invoked_with_queue and not all([playlist_prompt.update, playlist_prompt.url]):
            playlist_prompt.queue = True
            playlist_prompt.cancelled = False

        if playlist_prompt.cancelled:
            return
        if manageable and playlist_prompt.queue and any([playlist_prompt.update, playlist_prompt.url]):
            playlist_prompt.queue = False
        if manageable and playlist_prompt.delete:
            await context.send(
                embed=await context.pylav.construct_embed(
                    title=_("I have deleted a playlist."),
                    messageable=context,
                    description=_(
                        "{user_variable_do_not_translate}, playlist {playlist_name_variable_do_not_translate} has been deleted."
                    ).format(
                        user_variable_do_not_translate=context.author.mention,
                        playlist_name_variable_do_not_translate=await playlist.get_name_formatted(with_url=True),
                    ),
                ),
                ephemeral=True,
            )
            await playlist.delete()
            return
        tracks_added = 0
        tracks_removed = 0
        changed = False
        if manageable:
            if playlist_prompt.clear:
                changed = True
                await playlist.remove_all_tracks()
            if playlist_prompt.name and playlist_prompt.name != await playlist.fetch_name():
                changed = True
                await playlist.update_name(playlist_prompt.name)
            if playlist_prompt.url and playlist_prompt.url != await playlist.fetch_url():
                changed = True
                await playlist.update_url(playlist_prompt.url)
            if (playlist_prompt.add_tracks or playlist_prompt.remove_prompt) and not playlist_prompt.update:
                if playlist_prompt.remove_tracks:
                    response = await self.pylav.get_tracks(
                        *[await Query.from_string(at) for at in playlist_prompt.remove_tracks], player=context.player
                    )
                    match response.loadType:
                        case "track":
                            tracks = [response.data]
                        case "search":
                            tracks = response.data
                        case "playlist":
                            tracks = response.data.tracks
                        case __:
                            tracks = []
                    tracks = typing.cast(collections.deque[Track_namespace_conflict], tracks)
                    for t in tracks:
                        b64 = t.encoded
                        await playlist.remove_track(b64)
                        changed = True
                        tracks_removed += 1
                if playlist_prompt.add_tracks:
                    response = await self.pylav.get_tracks(
                        *[await Query.from_string(at) for at in playlist_prompt.add_tracks],
                        player=context.player,
                    )
                    match response.loadType:
                        case "track":
                            tracks = [response.data]
                        case "search":
                            tracks = response.data
                        case "playlist":
                            tracks = response.data.tracks
                        case __:
                            tracks = []
                    if tracks := typing.cast(collections.deque[Track_namespace_conflict], tracks):
                        await playlist.add_track(list(tracks))
                        changed = True
                        tracks_added += sum(1 for __ in tracks)
        if playlist_prompt.update:
            if url := await playlist.fetch_url():
                with contextlib.suppress(Exception):
                    response = await self.pylav.get_tracks(
                        await Query.from_string(url), bypass_cache=True, player=context.player
                    )
                    match response.loadType:
                        case "track":
                            tracks = [response.data]
                        case "search":
                            tracks = response.data
                        case "playlist":
                            tracks = response.data.tracks
                        case __:
                            tracks = []
                    if not tracks:
                        await context.send(
                            embed=await context.pylav.construct_embed(
                                messageable=context,
                                description=_(
                                    "Playlist {playlist_name_variable_do_not_translate} could not be updated with URL: {url_variable_do_not_translate}"
                                ).format(
                                    playlist_name_variable_do_not_translate=bold(
                                        await playlist.get_name_formatted(with_url=True)
                                    ),
                                    url_variable_do_not_translate=f"<{url}>",
                                ),
                            ),
                            ephemeral=True,
                        )
                        return
                    if b64_list := typing.cast(list[Track_namespace_conflict], [track for track in tracks if track.encoded]):  # type: ignore
                        changed = True
                        await playlist.update_tracks(b64_list)
            elif playlist.id in BUNDLED_PLAYLIST_IDS:
                changed = True
                if playlist.id < 1000001:
                    await self.pylav.playlist_db_manager.update_bundled_playlists(playlist.id)
                else:
                    await self.pylav.playlist_db_manager.update_bundled_external_playlists(playlist.id)
        if manageable:
            if playlist_prompt.dedupe:
                track = await playlist.fetch_tracks()
                new_tracks = [
                    from_dict(data_class=Track_namespace_conflict, data=json.loads(t))
                    for t in {json.dumps(d, sort_keys=True) for d in track}
                ]
                if diff := len(track) - len(new_tracks):
                    changed = True
                    await playlist.update_tracks(new_tracks)
                    tracks_removed += diff
            if playlist_prompt.queue:
                changed = True
                if context.player:
                    if tracks := context.player.queue.raw_queue:
                        queue_tracks = [track._processed for track in tracks if track.encoded]
                        await playlist.add_track(queue_tracks)
                        tracks_added += len(queue_tracks)

        if changed:
            extras = ""
            if tracks_removed:
                match tracks_removed:
                    case 1:
                        extras += _("\n1 track was removed from the playlist.")
                    case __:
                        extras += _(
                            "\n{track_count_variable_do_not_translate} tracks were removed from the playlist."
                        ).format(track_count_variable_do_not_translate=tracks_removed)
            if tracks_added:
                match tracks_added:
                    case 1:
                        extras += _("\n1 track was added to the playlist.")
                    case __:
                        extras += _(
                            "\n{track_count_variable_do_not_translate} tracks were added to the playlist."
                        ).format(track_count_variable_do_not_translate=tracks_added)
            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context,
                    title=_("Playlist updated"),
                    description=_(
                        "{user_variable_do_not_translate}, playlist {playlist_name_variable_do_not_translate} has been updated.{extras_variable_do_not_translate}."
                    ).format(
                        user_variable_do_not_translate=context.author.mention,
                        playlist_name_variable_do_not_translate=await playlist.get_name_formatted(with_url=True),
                        extras_variable_do_not_translate=extras,
                    ),
                ),
                ephemeral=True,
            )
        else:
            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context,
                    title=_("Playlist unchanged"),
                    description=_(
                        "{user_variable_do_not_translate}, playlist {playlist_name_variable_do_not_translate} has not been updated."
                    ).format(
                        user_variable_do_not_translate=context.author.mention,
                        playlist_name_variable_do_not_translate=await playlist.get_name_formatted(with_url=True),
                    ),
                ),
                ephemeral=True,
            )

    @slash_playlist.command(
        name="play",
        description=_("Enqueue a playlist"),
    )
    @app_commands.describe(
        playlist=_("The playlist to enqueue"),
        reverse=_("Set whether the playlist should be queued starting from the last track"),
    )
    @app_commands.guild_only()
    @invoker_is_dj(slash=True)
    async def slash_playlist_play(
        self,
        interaction: DISCORD_INTERACTION_TYPE,
        playlist: PlaylistConverter,
        reverse: bool = False,
    ):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await self.bot.get_context(interaction)

        playlists: list[Playlist] = playlist
        playlist = await maybe_prompt_for_playlist(cog=self, playlists=playlists, context=context)
        if not playlist:
            return
        await self.command_playlist_play.callback(self, context, playlist=[playlist], reverse=reverse)  # type: ignore

    @slash_playlist.command(
        name="delete",
        description=_("Delete a playlist"),
    )
    @app_commands.describe(playlist=_("The playlist to delete"))
    @app_commands.guild_only()
    async def slash_playlist_delete(self, interaction: DISCORD_INTERACTION_TYPE, playlist: PlaylistConverter):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await self.bot.get_context(interaction)

        playlists: list[Playlist] = playlist
        playlist = await maybe_prompt_for_playlist(cog=self, playlists=playlists, context=context)
        if not playlist:
            return
        if playlist.id not in BUNDLED_PLAYLIST_IDS:
            manageable = await playlist.can_manage(bot=self.bot, requester=context.author)
        else:
            manageable = False
        if not manageable:
            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context,
                    description=_(
                        "{user_variable_do_not_translate}, playlist {playlist_name_variable_do_not_translate} cannot be managed by yourself."
                    ).format(
                        user_variable_do_not_translate=context.author.mention,
                        playlist_name_variable_do_not_translate=await playlist.get_name_formatted(with_url=True),
                    ),
                ),
                ephemeral=True,
            )
            return
        await playlist.delete()
        await context.send(
            embed=await context.pylav.construct_embed(
                title=_("Playlist deleted"),
                messageable=context,
                description=_(
                    "{user_variable_do_not_translate}, playlist {playlist_name_variable_do_not_translate} has been deleted."
                ).format(
                    user_variable_do_not_translate=context.author.mention,
                    playlist_name_variable_do_not_translate=await playlist.get_name_formatted(with_url=True),
                ),
            ),
            ephemeral=True,
        )

    @slash_playlist.command(
        name="info",
        description=_("Display info about a playlist"),
    )
    @app_commands.describe(playlist=_("The playlist show info about"))
    @app_commands.guild_only()
    async def slash_playlist_info(self, interaction: DISCORD_INTERACTION_TYPE, playlist: PlaylistConverter):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await self.bot.get_context(interaction)

        playlists: list[Playlist] = playlist
        playlist = await maybe_prompt_for_playlist(cog=self, playlists=playlists, context=context)
        if not playlist:
            return

        await PaginatingMenu(
            bot=self.bot,
            cog=self,
            source=TrackMappingSource(
                guild_id=context.guild.id,
                cog=self,
                author=context.author,
                entries=await playlist.fetch_tracks(),
                playlist=playlist,
            ),
            delete_after_timeout=True,
            starting_page=0,
            original_author=context.author,
        ).start(context)

    @slash_playlist.command(name="save", description=_("Add the currently player queue to a playlist"))
    @app_commands.describe(playlist=_("The playlist to append the queue to"))
    @app_commands.guild_only()
    @requires_player(slash=True)
    async def slash_playlist_save(self, interaction: DISCORD_INTERACTION_TYPE, playlist: PlaylistConverter):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await self.bot.get_context(interaction)
        if not context.player.queue.size():
            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context,
                    title=_("Nothing to save"),
                    description=_("There is nothing in the queue to save."),
                ),
                ephemeral=True,
            )
            return
        playlists: list[Playlist] = playlist
        playlist = await maybe_prompt_for_playlist(cog=self, playlists=playlists, context=context)
        if not playlist:
            return
        if playlist.id not in BUNDLED_PLAYLIST_IDS:
            manageable = await playlist.can_manage(bot=self.bot, requester=context.author)
        else:
            manageable = False
        if not manageable:
            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context,
                    description=_(
                        "{user_variable_do_not_translate}, playlist {playlist_name_variable_do_not_translate} cannot be managed by yourself."
                    ).format(
                        user_variable_do_not_translate=context.author.mention,
                        playlist_name_variable_do_not_translate=await playlist.get_name_formatted(with_url=True),
                    ),
                ),
                ephemeral=True,
            )
            return
        tracks_added = 0
        changed = False
        if context.player:
            if tracks := context.player.queue.raw_queue:
                queue_tracks = [track._processed for track in tracks if track.encoded]
                await playlist.add_track(queue_tracks)
                tracks_added += len(queue_tracks)
                changed = True
        if changed:
            extras = ""
            if tracks_added:
                match tracks_added:
                    case 1:
                        extras += _("\n1 track was added to the playlist.")
                    case __:
                        extras += _(
                            "\n{track_count_variable_do_not_translate} tracks were added to the playlist."
                        ).format(
                            track_count_variable_do_not_translate=tracks_added,
                        )

            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context,
                    title=_("Playlist updated"),
                    description=_(
                        "{user_variable_do_not_translate}, playlist {playlist_name_variable_do_not_translate} has been updated.{extras_variable_do_not_translate}."
                    ).format(
                        user_variable_do_not_translate=context.author.mention,
                        playlist_name_variable_do_not_translate=await playlist.get_name_formatted(with_url=True),
                        extra_values=extras,
                    ),
                ),
                ephemeral=True,
            )

        else:
            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context,
                    title=_("Playlist unchanged"),
                    description=_(
                        "{user_variable_do_not_translate}, playlist {playlist_name_variable_do_not_translate} has not been updated"
                    ).format(
                        user_variable_do_not_translate=context.author.mention,
                        playlist_name_variable_do_not_translate=await playlist.get_name_formatted(with_url=True),
                    ),
                ),
                ephemeral=True,
            )

    @slash_playlist.command(name="upload", description=_("Upload a playlist to the bot"))
    @app_commands.describe(
        url=_("The URL of the playlist to upload"),
    )
    @app_commands.guild_only()
    @invoker_is_dj(slash=True)
    async def slash_playlist_upload(self, interaction: DISCORD_INTERACTION_TYPE, url: str = None):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        context = await self.bot.get_context(interaction)
        valid_playlist_urls = set()
        if url:
            if isinstance(url, str):
                url = url.strip("<>")
                valid_playlist_urls.add(url)
            else:
                valid_playlist_urls.update([r.strip("<>") for r in url])
        elif not context.message.attachments:
            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context,
                    description=_("You must either provide a URL or attach a playlist file to upload a playlist."),
                ),
                ephemeral=True,
            )
            return
        if context.message.attachments:
            for file in context.message.attachments:
                if file.filename.endswith(".pylav"):
                    valid_playlist_urls.add(file.url)
        if not valid_playlist_urls:
            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context, description=_("No valid playlist file provided")
                ),
                ephemeral=True,
            )
            return
        elif len(valid_playlist_urls) > 1:
            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context,
                    description=_("Multiple playlist files are provided. Currently, only 1 per message is allowed."),
                ),
                ephemeral=True,
            )
            return
        invalid_playlists_urls = set()
        saved_playlists = []
        for url in valid_playlist_urls:
            try:
                playlist = await Playlist.from_yaml(context=context, url=url, scope=context.author.id)
                saved_playlists.append(f"{bold(await playlist.fetch_name())} ({playlist.id})")
            except InvalidPlaylistException:
                invalid_playlists_urls.add(url)

        if not saved_playlists:
            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context, description=_("Failed to save any of the requested playlists.")
                ),
                ephemeral=True,
            )
            return
        if invalid_playlists_urls:
            await context.send(
                embed=await context.pylav.construct_embed(
                    messageable=context,
                    description=_(
                        "Failed to save the following playlists:\n{invalid_playlists_variable_do_not_translate}."
                    ).format(invalid_playlists_variable_do_not_translate=humanize_list(list(invalid_playlists_urls))),
                ),
                ephemeral=True,
            )
        await context.send(
            embed=await context.pylav.construct_embed(
                messageable=context,
                description=_(
                    "Successfully saved the following playlists:\n{saved_playlists_variable_do_not_translate}."
                ).format(saved_playlists_variable_do_not_translate=humanize_list(saved_playlists)),
            ),
            ephemeral=True,
        )

    @slash_playlist.command(name="mix", description=_("Play a YouTube mix playlist from a input"))
    @app_commands.describe(
        video=_("The YouTube video ID to play a mix from"),
        playlist=_("The YouTube playlist ID to play a mix from"),
        user=_("The YouTube user ID to play a mix from"),
        channel=_("The YouTube channel ID to play a mix from"),
    )
    @app_commands.guild_only()
    @invoker_is_dj(slash=True)
    async def slash_playlist_mix(
        self,
        context: DISCORD_INTERACTION_TYPE,
        video: str = None,
        playlist: str = None,
        user: str = None,
        channel: str = None,
    ):
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)
        if not any([video, playlist, user, channel]):
            await context.send(
                embed=await self.pylav.construct_embed(
                    description=_("You need to give me a parameter to use."),
                    messageable=context,
                ),
                ephemeral=True,
            )
            return
        player = self.pylav.get_player(context.guild.id)
        if player is None:
            config = self.pylav.player_config_manager.get_config(context.guild.id)
            if (channel := context.guild.get_channel_or_thread(await config.fetch_forced_channel_id())) is None:
                channel = rgetattr(context.author, "voice.channel", None)
                if not channel:
                    await context.send(
                        embed=await self.pylav.construct_embed(
                            description=_("You must be in a voice channel, so I can connect to it."),
                            messageable=context,
                        ),
                        ephemeral=True,
                    )
                    return
            if not (
                (permission := channel.permissions_for(context.guild.me)) and permission.connect and permission.speak
            ):
                await context.send(
                    embed=await self.pylav.construct_embed(
                        description=_(
                            "I do not have permission to connect or speak in {channel_name_variable_do_not_translate}."
                        ).format(channel_name_variable_do_not_translate=channel.mention),
                        messageable=context,
                    ),
                    ephemeral=True,
                )
                return
            player = await self.pylav.connect_player(channel=channel, requester=context.author)
        try:
            query = await Query.from_string(
                await self.pylav.generate_mix_playlist(
                    video_id=video, playlist_id=playlist, user_id=user, channel_id=channel
                )
            )
        except PyLavInvalidArgumentsException as e:
            await context.send(
                embed=await self.pylav.construct_embed(
                    description=str(e),
                    messageable=context,
                ),
                ephemeral=True,
            )
            return
        total_tracks_enqueue = 0
        single_track = None
        single_track, total_tracks_enqueue = await self._process_play_queries(
            context, [query], player, single_track, total_tracks_enqueue
        )
        if not (player.is_active or player.queue.empty()):
            await player.next(requester=context.author)

        await self._process_play_message(context, single_track, total_tracks_enqueue, [query])

    async def _process_play_message(self, context, single_track, total_tracks_enqueue, queries):
        artwork = None
        file = None
        match total_tracks_enqueue:
            case 1:
                if len(queries) == 1:
                    description = _(
                        "{track_name_variable_do_not_translate} enqueued for {service_variable_do_not_translate}."
                    ).format(
                        service_variable_do_not_translate=queries[0].source,
                        track_name_variable_do_not_translate=await single_track.get_track_display_name(with_url=True),
                    )
                elif len(queries) > 1:
                    description = _(
                        "{track_name_variable_do_not_translate} enqueued for {services_variable_do_not_translate}."
                    ).format(
                        services_variable_do_not_translate=humanize_list([q.source for q in queries]),
                        track_name_variable_do_not_translate=await single_track.get_track_display_name(with_url=True),
                    )
                else:
                    description = _("{track_name_variable_do_not_translate} enqueued.").format(
                        track_name_variable_do_not_translate=await single_track.get_track_display_name(with_url=True)
                    )
                artwork = await single_track.artworkUrl()
                file = await single_track.get_embedded_artwork()
            case 0:
                if len(queries) == 1:
                    description = _(
                        "No tracks were found for your query on {service_variable_do_not_translate}."
                    ).format(service_variable_do_not_translate=queries[0].source)
                elif len(queries) > 1:
                    description = _(
                        "No tracks were found for your queries on {services_variable_do_not_translate}."
                    ).format(services_variable_do_not_translate=humanize_list([q.source for q in queries]))
                else:
                    description = _("No tracks were found for your query.")
            case __:
                if len(queries) == 1:
                    description = _(
                        "{number_of_tracks_variable_do_not_translate} tracks enqueued for {service_variable_do_not_translate}."
                    ).format(
                        service_variable_do_not_translate=queries[0].source,
                        number_of_tracks_variable_do_not_translate=total_tracks_enqueue,
                    )
                elif len(queries) > 1:
                    description = _(
                        "{number_of_tracks_variable_do_not_translate} tracks enqueued for {services_variable_do_not_translate}."
                    ).format(
                        services_variable_do_not_translate=humanize_list([q.source for q in queries]),
                        number_of_tracks_variable_do_not_translate=total_tracks_enqueue,
                    )
                else:
                    description = _("{number_of_tracks_variable_do_not_translate} tracks enqueued.").format(
                        number_of_tracks_variable_do_not_translate=total_tracks_enqueue
                    )
        await context.send(
            embed=await self.pylav.construct_embed(
                description=description,
                thumbnail=artwork,
                messageable=context,
            ),
            ephemeral=True,
            file=file,
        )

    @commands.command(name="__command_playlist_play", hidden=True)
    @always_hidden()
    async def command_playlist_play(self, context: PyLavContext, *, playlist: PlaylistConverter, reverse: bool):
        if isinstance(context, discord.Interaction):
            context = await self.bot.get_context(context)
        if context.interaction and not context.interaction.response.is_done():
            await context.defer(ephemeral=True)
        playlists: list[Playlist] = playlist
        playlist = await maybe_prompt_for_playlist(cog=self, playlists=playlists, context=context)
        if not playlist:
            return
        if (player := context.player) is None:
            config = self.pylav.player_config_manager.get_config(context.guild.id)
            if (channel := context.guild.get_channel_or_thread(await config.fetch_forced_channel_id())) is None:
                channel = rgetattr(context, "author.voice.channel", None)
                if not channel:
                    await context.send(
                        embed=await context.pylav.construct_embed(
                            messageable=context,
                            description=_("You must be in a voice channel, so I can connect to it."),
                        ),
                        ephemeral=True,
                    )
                    return
            if not ((permission := channel.permissions_for(context.me)) and permission.connect and permission.speak):
                await context.send(
                    embed=await context.pylav.construct_embed(
                        description=_(
                            "I do not have permission to connect or speak in {channel_variable_do_not_translate}."
                        ).format(channel_variable_do_not_translate=channel.mention),
                        messageable=context,
                    ),
                    ephemeral=True,
                )
                return
            player = await context.connect_player(channel=channel)
        track_count = await playlist.size()

        tracks = await playlist.fetch_tracks()
        if reverse:
            tracks = list(reversed(tracks))
        track_objects = [from_dict(data_class=Track_namespace_conflict, data=track) for track in tracks]
        await player.bulk_add(
            requester=context.author.id,
            tracks_and_queries=[
                await Track.build_track(
                    node=player.node, data=track, requester=context.author.id, query=None, player_instance=player
                )
                for track in track_objects
            ],
        )
        bundle_prefix = _("Playlist")
        playlist_name = f"\n\n**{bundle_prefix}**:  {await playlist.get_name_formatted(with_url=True)}"
        await context.send(
            embed=await context.pylav.construct_embed(
                messageable=context,
                description=_(
                    "{track_count_variable_do_not_translate} tracks enqueued.{playlist_name_variable_do_not_translate}."
                ).format(
                    track_count_variable_do_not_translate=track_count,
                    playlist_name_variable_do_not_translate=playlist_name,
                ),
            ),
            ephemeral=True,
        )

        if not player.is_active:
            await player.next(requester=context.author)
