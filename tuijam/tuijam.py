#!/usr/bin/env python3
# coding=utf-8
from os.path import join, expanduser, isfile
from os import makedirs
from itertools import zip_longest
from getpass import getpass
from collections import deque  # needed for history migration
import sys
import pickle

import urwid
import gmusicapi
import logging
import yaml

import requests
import hashlib
from datetime import datetime

__version__ = "0.4.0"

WELCOME = '''
   ▄             ▀       ▀               
 ▄▄█▄▄  ▄   ▄  ▄▄▄     ▄▄▄    ▄▄▄   ▄▄▄▄▄
   █    █   █    █       █   ▀   █  █ █ █
   █    █   █    █       █   ▄▀▀▀█  █ █ █
   ▀▄▄  ▀▄▄▀█  ▄▄█▄▄     █   ▀▄▄▀█  █ █ █
                         █               
                       ▀▀                
         - A Google Music Player -       
'''  # noqa

CONFIG_DIR = join(expanduser('~'), '.config', 'tuijam')

RATE_UI = {
    0: '-',  # No rating
    1: '▼',  # Thumbs down
    2: '▼',  # Legacy
    3: '-',  # Legacy
    4: '▲',  # Legacy
    5: '▲',  # Thumbs up
}


class LastFMAPI:
    API_KEY = '5cc045ddea219f89adb7efec168d64ac'
    API_SECRET = '8397b63671b211c4e70f6ba1d8ea7825'
    API_ROOT_URL = 'http://ws.audioscrobbler.com/2.0/'
    USER_AGENT = 'TUIJam/'+__version__

    def __init__(self, sk=None):
        # Initialize session key with None
        self.sk = sk
        pass

    def call_method(self, method_name: str, params=None) -> dict:
        # Construct API request parameters dict
        if params is None:
            params = {}

        api_params = {
            'method': method_name,
            'api_key': LastFMAPI.API_KEY,
        }
        api_params.update(params)

        # Construct api_sig (https://www.last.fm/api/desktopauth#6)
        m = hashlib.md5()
        for key in sorted(api_params.keys()):
            m.update((key.encode('utf-8') + str(api_params[key]).encode('utf-8')))
        m.update(LastFMAPI.API_SECRET.encode('utf-8'))

        # Add api_sig to the request parameters
        api_params.update({'api_sig': m.hexdigest()})

        # Last.fm API docs don't even say that you DON'T need to put it in api_sig.
        # Unlike other methods, auth.getToken works with it.
        # Shame on you, Last.fm!
        api_params.update({'format': 'json'})

        r = requests.post(LastFMAPI.API_ROOT_URL,
                          params=api_params,
                          headers={'User-Agent': LastFMAPI.USER_AGENT})
        return r.json()

    def get_token(self):
        token_response = self.call_method('auth.getToken')
        token = None
        if not token_response.get('error'):
            token = token_response.get('token')
            return token
        else:
            # TODO throw an exception?
            return None

    def get_auth_url(self, token):
        return 'http://www.last.fm/api/auth/?api_key=%s&token=%s' % (LastFMAPI.API_KEY, token)

    def auth_by_token(self, token):
        response = self.call_method('auth.getSession', {'token': token})
        logging.warning('LASTFM: auth_by_token: ' + response.__str__())
        if response.get('error', False):
            return False
        self.sk = response.get('session').get('key')
        return True

    def update_now_playing(self, artist, track, album, duration):
        if self.sk is None:
            return
        response = self.call_method('track.updateNowPlaying', {
            'artist': artist,
            'track': track,
            'album': album,
            'duration': duration,
            'sk': self.sk
        })
        logging.warning('LASTFM: updateNowPlaying: ' + response.__str__())
        # TODO error handle

    def update_now_playing_song(self, song):
        try:
            self.update_now_playing(song.artist, song.title, song.album,
                                    song.length[1]*60 + song.length[0])
            song.lastfm_ts_start = int(datetime.now().timestamp())
            # ^ there could be a bug when tracks are scrobbled in the past or the future
            #   (depends on timezone)
        except Exception as e:
            logging.exception("LASTFM: updateNowPlaying: " + e.__str__())
            logging.error('LASTFM: updateNowPlaying: failed to update')

    def scrobble(self, artist, track, album, duration, ts_start):
        # See: https://www.last.fm/api/scrobbling#when-is-a-scrobble-a-scrobble
        # Minimum 30 seconds long + has been listened for min(50% of its length or 4 minutes)
        # See the scrobble method reference (https://www.last.fm/api/show/track.scrobble)
        if self.sk is None or duration < 30:
            return

        response = self.call_method('track.scrobble', {
            'timestamp[0]': str(ts_start),
            'artist[0]': artist,
            'track[0]': track,
            'album[0]': album,
            'duration[0]': duration,
            'sk': self.sk
        })
        logging.warning("LASTFM: scrobble: response = " + response.__str__())

    def scrobble_song(self, song):
        try:
            self.scrobble(song.artist, song.title, song.album,
                          song.length[1]*60 + song.length[0], song.lastfm_ts_start)
            song.lastfm_scrobbled = True
        except Exception as e:
            logging.exception("LASTFM: scrobble: " + e.__str__())
            logging.error('LASTFM: scrobble: failed to scrobble')



def sec_to_min_sec(sec_tot):
    s = int(sec_tot or 0)
    return s//60, s % 60


class MusicObject:

    @staticmethod
    def to_ui(*txts, weights=()):

        first, *rest = [(weight, str(txt)) for weight, txt in zip_longest(weights, txts, fillvalue=1)]
        items = [('weight', first[0], urwid.SelectableIcon(first[1], 0))]

        for weight, line in rest:
            items.append(('weight', weight, urwid.Text(line)))

        line = urwid.Columns(items)
        line = urwid.AttrMap(line, 'search normal', 'search select')

        return line

    @staticmethod
    def header_ui(*txts, weights=()):

        header = urwid.Columns(
            [('weight', weight, urwid.Text(('header', txt)))
             for weight, txt in zip_longest(weights, txts, fillvalue=1)])
        return urwid.AttrMap(header, 'header_bg')


class Song(MusicObject):
    ui_weights = (1, 2, 1, 0.2, 0.2)

    def __init__(self, title, album, albumId, albumArtRef, artist, artistId,
                 id_, type_, trackType, length, rating):
        self.title = title
        self.album = album
        self.albumId = albumId
        self.albumArtRef = albumArtRef
        self.artist = artist
        self.artistId = artistId
        self.id = id_
        self.type = type_
        self.trackType = trackType
        self.length = length
        self.rating = rating
        self.stream_url = ''

    def __repr__(self):
        return f'<Song title:{self.title}, album:{self.album}, artist:{self.artist}>'

    def __str__(self):
        return f'{self.title} by {self.artist}'

    def fmt_str(self):
        return [('np_song', f'{self.title} '), 'by ', ('np_artist', f'{self.artist}')]

    def ui(self):
        return self.to_ui(self.title, self.album, self.artist,
                          '{:d}:{:02d}'.format(*self.length), RATE_UI[self.rating],
                          weights=self.ui_weights)

    @classmethod
    def header(cls):
        return MusicObject.header_ui('Title', 'Album', 'Artist', 'Length', 'Rating', weights=cls.ui_weights)

    @staticmethod
    def from_dict(d):

        try:
            title = d['title']
            album = d['album']
            albumId = d['albumId']
            albumArtRef = d['albumArtRef'][0]['url']
            artist = d['artist']
            artistId = d['artistId'][0]

            try:
                id_ = d['id']
                type_ = 'library'

            except KeyError:
                id_ = d['storeId']
                type_ = 'store'

            trackType = d.get('trackType', None)
            length = sec_to_min_sec(int(d['durationMillis']) / 1000)

            # rating scheme
            #  0 - No Rating
            #  1 - Thumbs down
            #  5 - Thumbs up

            rating = int(d.get('rating', 0))
            return Song(title, album, albumId, albumArtRef, artist, artistId, id_, type_, trackType, length, rating)

        except KeyError as e:
            logging.exception(f"Missing Key {e} in dict \n{d}")


class YTVideo(MusicObject):
    ui_weights = (4, 1)

    def __init__(self, title, channel, thumbnail, id_):

        self.title = title
        self.channel = channel
        self.thumbnail = thumbnail
        self.id = id_
        self.stream_url = ''

    def __repr__(self):
        return f'<YTVideo title:{self.title}, channel:{self.artist}>'

    def __str__(self):
        return f'{self.title} by {self.channel}'

    def fmt_str(self):
        return [('np_song', f'{self.title} '), 'by ', ('np_artist', f'{self.channel}')]

    def ui(self):
        return self.to_ui(self.title, self.channel, weights=self.ui_weights)

    @classmethod
    def header(cls):
        return MusicObject.header_ui('Youtube', 'Channel', weights=cls.ui_weights)

    @staticmethod
    def from_dict(d):

        try:
            title = d['snippet']['title']
            thumbnail = d['snippet']['thumbnails']['medium']['url']
            channel = d['snippet']['channelTitle']
            id_ = d['id']['videoId']

            return YTVideo(title, channel, thumbnail, id_)

        except KeyError as e:
            logging.exception(f"Missing Key {e} in dict \n{d}")


class Album(MusicObject):

    def __init__(self, title, artist, artistId, year, id_):

        self.title = title
        self.artist = artist
        self.artistId = artistId
        self.year = year
        self.id = id_

    def __repr__(self):
        return f'<Album title:{self.title}, artist:{self.artist}, year:{self.year}>'

    def ui(self):
        return self.to_ui(self.title, self.artist, self.year)

    @staticmethod
    def header():
        return MusicObject.header_ui('Album', 'Artist', 'Year')

    @staticmethod
    def from_dict(d):
        try:
            try:
                title = d['name']
            except KeyError:
                title = d['title']

            try:
                artist = d['albumArtist']
                artistId = d['artistId'][0]
            except KeyError:
                artist = d['artist_name']
                artistId = d['artist_metajam_id']

            try:
                year = d['year']
            except KeyError:
                year = ''

            try:
                id_ = d['albumId']
            except KeyError:
                id_ = d['id']['metajamCompactKey']

            return Album(title, artist, artistId, year, id_)

        except KeyError as e:
            logging.exception(f"Missing Key {e} in dict \n{d}")


class Artist(MusicObject):

    def __init__(self, name, id_):
        self.name = name
        self.id = id_

    def __repr__(self):
        return f'<Artist name:{self.name}>'

    def ui(self):
        return self.to_ui(self.name)

    @staticmethod
    def header():
        return MusicObject.header_ui('Artist')

    @staticmethod
    def from_dict(d):
        try:
            name = d['name']
            id_ = d['artistId']

            return Artist(name, id_)

        except KeyError as e:

            logging.exception(f"Missing Key {e} in dict \n{d}")


class Situation(MusicObject):
    ui_weights = (0.2, 1)

    def __init__(self, title, description, id_, stations):
        self.title = title
        self.description = description
        self.id = id_
        self.stations = stations

    def __repr__(self):
        return f'<Situation title:{self.title}>'

    def ui(self):
        return self.to_ui(self.title, self.description)

    @staticmethod
    def header():
        return MusicObject.header_ui('Situation', 'Description')

    @staticmethod
    def from_dict(d):

        try:

            title = d['title']
            description = d['description']
            id_ = d['id']
            situations = [d]
            stations = []

            while situations:

                situation = situations.pop()

                if 'situations' in situation:
                    situations.extend(situation['situations'])
                else:
                    stations.extend([
                        RadioStation(
                            station['name'], [],
                            id_=station['seed']['curatedStationId']
                        )
                        for station in situation['stations']
                    ])

            return Situation(title, description, id_, stations)

        except KeyError as e:
            logging.exception(f"Missing Key {e} in dict \n{d}")


class RadioStation(MusicObject):

    def __init__(self, title, seeds, id_=None):
        self.title = title
        self.seeds = seeds
        self.id = id_

    def __repr__(self):
        return f'<RadioStation title:{self.title}>'

    def ui(self):
        return self.to_ui(self.title)

    def get_station_id(self, api):
        if self.id:
            return api.create_station(self.title, curated_station_id=self.id)
        else:
            seed = self.seeds[0]
            return api.create_station(self.title, artist_id=seed['artistId'])

    @staticmethod
    def header():
        return MusicObject.header_ui('Station Name')

    @staticmethod
    def from_dict(d):

        try:
            title = d['title']
            seeds = d['id']['seeds']

            return RadioStation(title, seeds)

        except KeyError as e:
            logging.exception(f"Missing Key {e} in dict \n{d}")


class Playlist(MusicObject):
    ui_weights = (.4, 1)

    def __init__(self, name, songs=None, id_=None):
        self.name = name
        self.songs = songs
        self.id = id_

    def __repr__(self):
        return f'<Playlist name:{self.name}>'

    def ui(self):
        return self.to_ui(self.name, str(len(self.songs)), weights=self.ui_weights)

    @classmethod
    def header(cls):
        return MusicObject.header_ui('Playlist Name', '# Songs', weights=cls.ui_weights)

    @staticmethod
    def from_dict(d):

        try:
            name = d['name']
            id_ = d['id']
            songs = [Song.from_dict(song['track']) for song in d['tracks'] if 'track' in song]

            if songs:
                return Playlist(name, songs, id_)

        except KeyError as e:
            logging.exception(f"Missing Key {e} in dict \n{d}")


class SearchInput(urwid.Edit):

    def __init__(self, app):
        self.app = app
        super().__init__('search > ', multiline=False, allow_tab=False)

    def keypress(self, size, key):

        if key == 'enter':
            txt = self.edit_text

            if txt:
                self.set_edit_text('')
                self.app.search(txt)

            else:
                self.app.listen_now()

        else:
            size = (size[0],)
            return super().keypress(size, key)


class SearchPanel(urwid.ListBox):

    def __init__(self, app):

        self.app = app
        self.walker = urwid.SimpleFocusListWalker([])
        self.search_history = []
        self.search_results = ([], [], [], [], [], [], [])
        self.line_box = None
        self.viewing_previous_songs = False

        super().__init__(self.walker)

        self.walker.append(urwid.Text(WELCOME, align='center'))

    def keypress(self, size, key):

        if key == 'q' or key == 'Q':

            add_to_front = key == 'Q'
            selected = self.selected_search_obj()

            if not selected:
                return

            if type(selected) == Song or type(selected) == YTVideo:
                self.app.queue_panel.add_song_to_queue(selected, add_to_front)

            elif type(selected) == Album:
                self.app.queue_panel.add_album_to_queue(selected, add_to_front)

            elif type(selected) == RadioStation:
                radio_song_list = self.app.get_radio_songs(selected.get_station_id(self.app.g_api))

                if add_to_front:
                    radio_song_list = reversed(radio_song_list)

                for song in radio_song_list:
                    self.app.queue_panel.add_song_to_queue(song, add_to_front)

            elif type(selected) == Playlist:
                self.app.queue_panel.add_songs_to_queue(selected.songs, add_to_front)

        elif key in ('e', 'enter'):
            if self.selected_search_obj() is not None:
                self.app.expand(self.selected_search_obj())

        elif key == 'backspace':
            self.back()

        elif key == 'r':
            if self.selected_search_obj() is not None:
                self.app.create_radio_station(self.selected_search_obj())

        elif key == 'j':
            super().keypress(size, 'down')

        elif key == 'k':
            super().keypress(size, 'up')

        else:
            super().keypress(size, key)

    def back(self):

        if self.search_history:

            prev_focus, search_history = self.search_history.pop()

            self.set_search_results(*search_history)
            self.viewing_previous_songs = False
            self.line_box.set_title("Search Results")

            try:
                self.set_focus(prev_focus)
            except:
                pass

    def update_search_results(self, songs, albums, artists, situations, radio_stations, playlists, yt_vids, title="Search Results", isprevsong=False):

        if not self.viewing_previous_songs:  # only remember search history
            self.search_history.append((self.get_focus()[1], self.search_results))

        self.viewing_previous_songs = isprevsong

        self.set_search_results(songs, albums, artists, situations, radio_stations, playlists, yt_vids)
        self.line_box.set_title(title)

    def view_previous_songs(self, songs, yt_vids):
        self.update_search_results(songs, [], [], [], [], [], yt_vids, "Previous Songs", True)

    def set_search_results(self, songs, albums, artists, situations, radio_stations, playlists, yt_vids):

        def filter_none(lst, fsize=30):

            filtered = [obj for obj in lst if obj is not None]

            if self.viewing_previous_songs:
                return filtered
            else:
                return filtered[:fsize] #View fixed "fsize" elements if yo not in search history

        songs = filter_none(songs)
        albums = filter_none(albums)
        artists = filter_none(artists)
        situations = filter_none(situations)
        radio_stations = filter_none(radio_stations)
        playlists = filter_none(playlists)
        yt_vids = filter_none(yt_vids)

        self.search_results = (songs, albums, artists, situations, radio_stations, playlists, yt_vids)

        self.walker.clear()

        for group in [artists, albums, songs, situations, radio_stations, playlists, yt_vids]:

            if group:
                self.walker.append(type(group[0]).header())

            for item in group:
                self.walker.append(item.ui())

        if self.walker:
            self.walker.set_focus(1)

    def selected_search_obj(self):

        focus_id = self.walker.get_focus()[1]
        songs, albums, artists, situations, radio_stations, playlists, yt_vids = self.search_results

        try:
            for group in [artists, albums, songs, situations, radio_stations, playlists, yt_vids]:
                if group:
                    focus_id -= 1

                    if focus_id < len(group):
                        return group[focus_id]

                    focus_id -= len(group)

        except (IndexError, TypeError):
            pass


class PlayBar(urwid.ProgressBar):
    vol_inds = [' ', '▁', '▂', '▃', '▄', '▅', '▆', '▇', '█']

    def __init__(self, app, *args, **kwargs):

        super(PlayBar, self).__init__(*args, **kwargs)
        self.app = app

    def get_prog_tot(self):

        progress = self.app.player.time_pos or 0
        remaining = self.app.player.time_remaining or 0
        total = progress + remaining

        return progress, total

    def get_text(self):
        if self.app.current_song is None:
            return 'Idle'

        progress, total = self.get_prog_tot()
        song = self.app.current_song
        rating = ''

        if type(song) == Song:
            artist = song.artist

            if song.rating in (1, 5):
                rating = '('+RATE_UI[song.rating]+')'

        else:  # YTVideo
            artist = song.channel

        return ' {} {} - {} {}[{:d}:{:02d} / {:d}:{:02d}] {}'.format(
            ['■', '▶'][self.app.play_state == 'play'],
            artist,
            self.app.current_song.title,
            rating,
            *sec_to_min_sec(progress),
            *sec_to_min_sec(total),
            self.vol_inds[self.app.volume],
        )

    def update(self):
        self._invalidate()

        progress, total = self.get_prog_tot()
        if progress >= 0 and total > 0:
            percent = progress / total * 100
            self.set_completion(percent)
            song = self.app.current_song
            if self.app.lastfm_enabled and type(song) == Song:
                if not song.lastfm_scrobbled and total >= 30 and (percent > 50 or progress > 4*60):
                    self.app.lastfm.scrobble_song(song)

        else:
            self.set_completion(0)


class QueuePanel(urwid.ListBox):

    def __init__(self, app):

        self.app = app
        self.walker = urwid.SimpleFocusListWalker([])
        self.queue = []
        super().__init__(self.walker)

    def add_song_to_queue(self, song, to_front=False):

        if song:

            if to_front:
                self.queue.insert(0, song)
                self.walker.insert(0, song.ui())

            else:
                self.queue.append(song)
                self.walker.append(song.ui())

    def add_songs_to_queue(self, songs, to_front=False):

        song_list = reversed(songs) if to_front else songs

        for song in song_list:
            self.add_song_to_queue(song, to_front)

    def add_album_to_queue(self, album, to_front=False):

        album_info = self.app.g_api.get_album_info(album.id)
        track_list = reversed(album_info['tracks']) if to_front else album_info['tracks']

        for track in track_list:
            song = Song.from_dict(track)
            self.add_song_to_queue(song, to_front)

    def drop(self, idx):

        if 0 <= idx < len(self.queue):
            self.queue.pop(idx)
            self.walker.pop(idx)

    def clear(self):
        self.queue.clear()
        self.walker.clear()

    def swap(self, idx1, idx2):

        if (0 <= idx1 < len(self.queue)) and (0 <= idx2 < len(self.queue)):

            obj1, obj2 = self.queue[idx1], self.queue[idx2]
            self.queue[idx1], self.queue[idx2] = obj2, obj1

            ui1, ui2 = self.walker[idx1], self.walker[idx2]
            self.walker[idx1], self.walker[idx2] = ui2, ui1

    def to_top(self, idx):

        if 0 <= idx < len(self.queue):
            obj = self.queue[idx]
            del self.queue[idx]
            self.queue = [obj] + self.queue

            ui = self.walker[idx]
            del self.walker[idx]
            self.walker.insert(0, ui)

    def to_bottom(self, idx):

        if 0 <= idx < len(self.queue):
            obj = self.queue[idx]
            del self.queue[idx]
            self.queue.append(obj)

            ui = self.walker[idx]
            del self.walker[idx]
            self.walker.append(ui)

    def shuffle(self):
        from random import shuffle
        shuffle(self.queue)

        self.walker.clear()
        for s in self.queue:
            self.walker.append(s.ui())

    def play_next(self):

        if self.walker:
            self.walker.pop(0)
            next_song = self.queue.pop(0)

            logging.warning('LASTFM: play_next: scrobble enabled = ' + str(self.app.lastfm_enabled))
            logging.warning('LASTFM: play_next: song type = ' + str(type(next_song)))
            next_song.lastfm_scrobbled = False
            if self.app.lastfm_enabled and type(next_song) == Song:
                self.app.lastfm.update_now_playing_song(next_song)
            self.app.play(next_song)

        else:
            self.app.current_song = None
            self.app.stop()

    def selected_queue_obj(self):

        try:
            focus_id = self.walker.get_focus()[1]
            return self.queue[focus_id]

        except (IndexError, TypeError):
            return

    def keypress(self, size, key):
        focus_id = self.walker.get_focus()[1]

        if focus_id is None:
            return super().keypress(size, key)

        if key in ('u', 'shift up'):
            self.swap(focus_id, focus_id-1)
            self.keypress(size, 'up')

        elif key in ('d', 'shift down'):
            self.swap(focus_id, focus_id+1)
            self.keypress(size, 'down')

        elif key in ('v', 'U', 'ctrl up'):
            self.to_top(focus_id)
            self.walker.set_focus(0)

        elif key in ('D', 'ctrl down'):
            self.to_bottom(focus_id)
            self.walker.set_focus(len(self.walker)-1)

        elif key in ('right'):
            self.play_next()

        elif key in ('delete', 'x'):
            self.drop(focus_id)

        elif key == 'j':
            super().keypress(size, 'down')

        elif key == 'k':
            super().keypress(size, 'up')

        elif key == 'e':
            self.app.expand(self.selected_queue_obj())

        elif key == ' ':

            if self.app.play_state == 'stop':
                self.play_next()
            else:
                self.app.toggle_play()

        else:
            return super().keypress(size, key)


class App(urwid.Pile):

    palette = [
        ('header',             '', '', '', '#FFF,underline', ''),
        ('header_bg',          '', '', '', '#FFF',           ''),
        ('line',               '', '', '', '#FFF',           '',),
        ('search normal',      '', '', '', '#FFF',           ''),
        ('search select',      '', '', '', '#FFF',           '#D32'),

        ('region_bg normal',   '', '', '', '#888',           ''),
        ('region_bg select',   '', '', '', '#FFF',           ''),

        ('progress',           '', '', '', '#FFF',           '#D32'),
        ('progress_remaining', '', '', '', '#FFF',           '#444'),
    ]

    def __init__(self):

        import mpv
        self.player = mpv.MPV()
        self.player.volume = 100
        self.player['vid'] = 'no'
        self.volume = 8
        self.g_api = None
        self.loop = None
        self.config_pw = None
        self.reached_end_of_track = False
        self.lastfm_enabled = False
        self.lastfm = None

        @self.player.event_callback('end_file')
        def end_file_callback(event):

            if event['event']['reason'] == 0:

                self.reached_end_of_track = True
                if self.lastfm_enabled:
                    self.current_song.lastfm_scrobbled = False
                self.schedule_refresh(dt=0.01)

        self.search_panel = SearchPanel(self)
        search_panel_wrapped = urwid.LineBox(self.search_panel, title='Search Results')

        # Give search panel reference to LineBox to change the title dynamically
        self.search_panel.line_box = search_panel_wrapped

        search_panel_wrapped = urwid.AttrMap(search_panel_wrapped, 'region_bg normal', 'region_bg select')
        self.search_panel_wrapped = search_panel_wrapped

        self.playbar = PlayBar(self, 'progress_remaining', 'progress', current=0, done=100)

        self.queue_panel = QueuePanel(self)
        queue_panel_wrapped = urwid.LineBox(self.queue_panel, title='Queue')

        queue_panel_wrapped = urwid.AttrMap(queue_panel_wrapped, 'region_bg normal', 'region_bg select')
        self.queue_panel_wrapped = queue_panel_wrapped

        self.search_input = urwid.Edit('> ', multiline=False)
        self.search_input = SearchInput(self)

        urwid.Pile.__init__(self, [('weight', 12, search_panel_wrapped),
                                   ('pack', self.playbar),
                                   ('weight', 7, queue_panel_wrapped),
                                   ('pack', self.search_input)
                                   ])

        self.set_focus(self.search_input)

        self.play_state = 'stop'
        self.current_song = None
        self.history = []

    def login(self):

        self.g_api = gmusicapi.Mobileclient(debug_logging=False)
        credentials = self.read_config()

        if credentials is None:
            return False

        else:
            return self.g_api.login(*credentials)

    def read_config(self):

        config_file = join(CONFIG_DIR, 'config.yaml')

        if not isfile(config_file):
            if not self.first_time_setup():
                return

        email, password, device_id = None, None, None
        with open(config_file) as f:

            config = yaml.safe_load(f.read())

            if config.get('encrypted', False):

                from scrypt import decrypt

                if self.config_pw is not None:
                    config_pw = self.config_pw  # From first_time_setup

                else:
                    config_pw = getpass("Enter tuijam config pw: ")

                try:
                    email = decrypt(config['email'], config_pw, maxtime=20)
                    password = decrypt(config['password'], config_pw, maxtime=20)
                    device_id = decrypt(config['device_id'], config_pw, maxtime=20)
                    lastfm_sk_encrypted = config.get('lastfm_sk', None)
                    self.lastfm_sk = None
                    if lastfm_sk_encrypted:
                        self.lastfm_sk = decrypt(lastfm_sk_encrypted, config_pw, maxtime=20)

                except Exception as e:

                    print(e)
                    print("Could not decrypt config file.")
                    exit(1)

            else:
                email = config['email']
                password = config['password']
                device_id = config['device_id']
                self.lastfm_sk = config.get('lastfm_sk', None)

            self.mpris_enabled = config.get('mpris_enabled', True)
            self.persist_queue = config.get('persist_queue', True)
            self.reverse_scrolling = config.get('reverse_scrolling', False)
            self.video = config.get('video', False)

            if self.lastfm_sk is not None:
                self.lastfm = LastFMAPI(self.lastfm_sk)
                # TODO handle if sk is invalid
                self.lastfm_enabled = True

        return email, password, device_id

    def first_time_setup(self):

        print("Need to perform first time setup")

        while True:

            email = input("Enter gmusic email (empty to quit): ")
            if not email:
                return False

            pw = getpass("Enter gmusic password: ")

            d_id = self.get_device_id(email, pw)

            if d_id is not None:
                break  # Success!

            print(('Login failed, verify your email and password.\n'
                   'Remember, you need an app-password if you have 2FA enabled.'))

        print("Enter a password to encrypt/decrypt the generated config file.")
        print("You will need to enter this each time you start tuijam.")
        print("If you forget this, delete the config file and start tuijam.")
        print("Leave blank if no encryption is desired.")

        self.config_pw = getpass("password: ")

        print()
        self.write_config(email, pw, d_id, self.config_pw)
        return True

    @staticmethod
    def write_config(email, password, device_id, config_pw):
        use_encryption = len(config_pw) > 0

        if use_encryption:

            from scrypt import encrypt
            data = dict(
                encrypted=True,
                email=encrypt(email, config_pw, maxtime=0.5),
                password=encrypt(password, config_pw, maxtime=0.5),
                device_id=encrypt(device_id, config_pw, maxtime=0.5)
            )

        else:

            data = dict(
                encrypted=False,
                email=email,
                password=password,
                device_id=device_id
            )

        data['mpris_enabled'] = True
        data['persist_queue'] = True
        data['reverse_scrolling'] = False
        data['video'] = False

        config_file = join(CONFIG_DIR, 'config.yaml')

        with open(config_file, "w") as outfile:
            yaml.safe_dump(data, outfile, default_flow_style=False)

    def get_device_id(self, email, password):

        if not self.g_api.login(email, password, self.g_api.FROM_MAC_ADDRESS):
            return

        ids = [d['id'][2:] if d['id'].startswith('0x') else d['id'].replace(':', '')
               for d in self.g_api.get_registered_devices()]

        self.g_api.logout()

        try:
            return ids[0]

        except IndexError:
            print("No device ids found. This shouldn't happen...")
            return

    def refresh(self, *args, **kwargs):

        if self.play_state == 'play' and self.reached_end_of_track:
            self.reached_end_of_track = False
            self.queue_panel.play_next()

        self.playbar.update()
        self.loop.draw_screen()

        if self.play_state == 'play':
            self.schedule_refresh()

    def schedule_refresh(self, dt=0.5):
        self.loop.set_alarm_in(dt, self.refresh)

    def play(self, song):

        self.current_song = song
        self.player.pause = True

        if type(song) == Song:
            self.current_song.stream_url = self.g_api.get_stream_url(song.id)

        else:  # YTVideo
            self.current_song.stream_url = f'https://youtu.be/{song.id}'

        self.player.play(self.current_song.stream_url)
        self.player.pause = False
        self.play_state = 'play'
        self.playbar.update()
        self.history.insert(0, song)
        self.history = self.history[:100]
        self.schedule_refresh()

        if self.mpris_enabled:
            self.mpris.emit_property_changed("PlaybackStatus")
            self.mpris.emit_property_changed("Metadata")

    def stop(self):

        try:
            self.player.pause = True
            self.player.seek(0, reference='absolute')

        except SystemError:  # seek throws error if there is no current song in mpv
            pass

        self.play_state = 'stop'
        self.playbar.update()

        if self.mpris_enabled:
            self.mpris.emit_property_changed("PlaybackStatus")

    def seek(self, dt):
        try:
            self.player.seek(dt)

        except SystemError:
            pass

        self.playbar.update()

    def toggle_play(self):

        if self.play_state == 'play':
            self.player.pause = True
            self.play_state = 'pause'
            self.playbar.update()

        elif self.play_state == 'pause' or (self.play_state == 'stop' and self.current_song is not None):
            self.player.pause = False
            self.play_state = 'play'
            self.playbar.update()
            self.schedule_refresh()

        elif self.play_state == 'stop':
            self.queue_panel.play_next()

        if self.mpris_enabled:
            self.mpris.emit_property_changed("PlaybackStatus")

    def volume_down(self):

        self.volume = max([0, self.volume-1])
        self.player.volume = int(self.volume * 100 / 8)
        self.playbar.update()

        if self.mpris_enabled:
            self.mpris.emit_property_changed("Volume")

    def volume_up(self):

        self.volume = min([8, self.volume+1])
        self.player.volume = int(self.volume * 100 / 8)
        self.playbar.update()

        if self.mpris_enabled:
            self.mpris.emit_property_changed("Volume")

    def keypress(self, size, key):

        if key == 'tab':
            current_focus = self.focus

            if current_focus == self.search_panel_wrapped:
                self.set_focus(self.queue_panel_wrapped)

            elif current_focus == self.queue_panel_wrapped:
                self.set_focus(self.search_input)

            else:
                self.set_focus(self.search_panel_wrapped)

        elif key == 'shift tab':

            current_focus = self.focus
            if current_focus == self.search_panel_wrapped:
                self.set_focus(self.search_input)

            elif current_focus == self.queue_panel_wrapped:
                self.set_focus(self.search_panel_wrapped)

            else:
                self.set_focus(self.queue_panel_wrapped)

        elif key == 'ctrl p':
            self.toggle_play()

        elif key == 'ctrl k':
            self.stop()

        elif key == 'ctrl n':
            self.queue_panel.play_next()

        elif key == 'ctrl r':
            hist_songs = [item for item in self.history if type(item) == Song]
            hist_yt = [item for item in self.history if type(item) == YTVideo]
            self.search_panel.view_previous_songs(hist_songs, hist_yt)

        elif key == 'ctrl s':
            self.queue_panel.shuffle()

        elif key == 'ctrl u':
            self.rate_current_song(5)

        elif key == 'ctrl d':
            self.rate_current_song(1)

        elif key == 'ctrl w':
            self.queue_panel.clear()

        elif key == 'ctrl q':
            self.queue_panel.add_songs_to_queue(self.search_panel.search_results[0])

        elif self.focus != self.search_input:
            if key == '>':
                self.seek(10)

            elif key == '<':
                self.seek(-10)

            elif key in '-_':
                self.volume_down()

            elif key in '+=':
                self.volume_up()

            elif key in ('/', 'ctrl f'):
                self.set_focus(self.search_input)

            else:
                return self.focus.keypress(size, key)

        else:
            return self.focus.keypress(size, key)

    def mouse_event(self, size, event, button, col, row, focus=True):

        up, down = [("up", "down"), ("down", "up")][self.reverse_scrolling]
        if button == 5:
            self.keypress(size, up)

        elif button == 4:
            self.keypress(size, down)

        else:
            super().mouse_event(size, event, button, col, row, focus=focus)

    def expand(self, obj):
        if obj is None:
            return

        songs, albums, artists, situations, radio_stations, playlists, yt_vids = [[]]*7

        if type(obj) == Song:
            album_info = self.g_api.get_album_info(obj.albumId)

            songs = [Song.from_dict(track) for track in album_info['tracks']]
            albums = [Album.from_dict(album_info)]
            artists = [Artist(obj.artist, obj.artistId)]

        elif type(obj) == Album:
            album_info = self.g_api.get_album_info(obj.id)

            songs = [Song.from_dict(track) for track in album_info['tracks']]
            albums = [obj]
            artists = [Artist(obj.artist, obj.artistId)]

        elif type(obj) == Artist:
            artist_info = self.g_api.get_artist_info(obj.id)

            songs = [Song.from_dict(track) for track in artist_info['topTracks']]
            albums = [Album.from_dict(album) for album in artist_info.get('albums', [])]
            artists = [Artist.from_dict(artist) for artist in artist_info['related_artists']]
            artists.insert(0, obj)

        elif type(obj) == Situation:
            radio_stations = obj.stations
            situations = [obj]

        elif type(obj) == RadioStation:
            station_id = obj.get_station_id(self.g_api)
            songs = self.get_radio_songs(station_id)
            radio_stations = [obj]

        elif type(obj) == Playlist:
            songs = obj.songs
            playlists = [obj]

        elif type(obj) == YTVideo:
            yt_vids = [obj]

        self.search_panel.update_search_results(songs, albums, artists, situations,
                                                radio_stations, playlists, yt_vids)

    def youtube_search(self, q, max_results=50, order="relevance", token=None, location=None, location_radius=None):

        '''
        Mostly stolen from: https://github.com/spnichol/youtube_tutorial/blob/master/youtube_videos.py
        '''

        from apiclient.discovery import build
        developer_key = "AIzaSyBtETg1PDC124WUAZ5JhJH_pu2xboHVIS0"

        youtube = build('youtube', 'v3', developerKey=developer_key)

        search_response = youtube.search().list(
            q=q,
            type="video",
            pageToken=token,
            order=order,
            part="id,snippet",
            maxResults=max_results,
            location=location,
            locationRadius=location_radius
        ).execute()

        videos = []
        for search_result in search_response.get("items", []):

            if search_result["id"]["kind"] == "youtube#video":
                videos.append(search_result)

        nexttok = search_response.get("nextPageToken", None)
        return (nexttok, videos)

    def search(self, query):

        results = self.g_api.search(query)

        songs = [Song.from_dict(hit['track']) for hit in results['song_hits']]
        albums = [Album.from_dict(hit['album']) for hit in results['album_hits']]
        artists = [Artist.from_dict(hit['artist']) for hit in results['artist_hits']]
        ytvids = [YTVideo.from_dict(hit) for hit in self.youtube_search(query)[1]]

        self.search_panel.update_search_results(songs, albums, artists, [], [], [], ytvids)
        self.set_focus(self.search_panel_wrapped)

    def listen_now(self):

        situations = self.g_api.get_listen_now_situations()
        items = self.g_api.get_listen_now_items()
        playlists = self.g_api.get_all_user_playlist_contents()
        liked = self.g_api.get_promoted_songs()

        situations = [Situation.from_dict(hit) for hit in situations]
        albums = [Album.from_dict(hit['album']) for hit in items if 'album' in hit]
        radio_stations = [RadioStation.from_dict(hit['radio_station']) for hit in items if 'radio_station' in hit]
        playlists = [Playlist.from_dict(playlist) for playlist in playlists]

        liked = [Song.from_dict(song) for song in liked]
        playlists.append(Playlist('Liked', liked, None))

        self.search_panel.update_search_results([], albums, [], situations, radio_stations, playlists, [])
        self.set_focus(self.search_panel_wrapped)

    def create_radio_station(self, obj):

        if type(obj) == Song:
            station_id = self.g_api.create_station(obj.title, track_id=obj.id)

        elif type(obj) == Album:
            station_id = self.g_api.create_station(obj.title, album_id=obj.id)

        elif type(obj) == Artist:
            station_id = self.g_api.create_station(obj.name, artist_id=obj.id)

        elif type(obj) == RadioStation:
            station_id = obj.get_station_id(self.g_api)

        else:
            return

        for song in self.get_radio_songs(station_id):
            self.queue_panel.add_song_to_queue(song)

    def get_radio_songs(self, station_id, n=50):

        song_dicts = self.g_api.get_station_tracks(station_id, num_tracks=n)
        return [Song.from_dict(song_dict) for song_dict in song_dicts]

    def rate_current_song(self, rating):

        if type(self.current_song) != Song:
            return

        if self.current_song.rating == rating:
            rating = 0

        self.current_song.rating = rating
        track = {}

        if self.current_song.type == 'library':
            track['id'] = self.current_song.id

        else:
            track['nid'] = self.current_song.id
            track['trackType'] = self.current_song.trackType

        self.g_api.rate_songs(track, rating)
        self.playbar.update()
        self.loop.draw_screen()

    def cleanup(self, *args, **kwargs):

        self.player.quit()
        del self.player

        self.g_api.logout()
        self.loop.stop()

        if self.persist_queue:
            self.save_queue()

        self.save_hist()
        sys.exit()

    def save_queue(self):

        print("saving queue")
        queue = []

        if self.current_song is not None:
            queue.append(self.current_song)

        queue.extend(self.queue_panel.queue)

        with open(join(CONFIG_DIR, 'queue.p'), 'wb') as f:
            pickle.dump(queue, f)

    def restore_queue(self):

        try:
            with open(join(CONFIG_DIR, 'queue.p'), 'rb') as f:
                self.queue_panel.add_songs_to_queue(pickle.load(f))

        except (AttributeError, FileNotFoundError, pickle.UnpicklingError):
            print("failed to restore queue. :(")
            self.queue_panel.clear()

    def save_hist(self):

        with open(join(CONFIG_DIR, 'hist.p'), 'wb') as f:
            pickle.dump(self.history, f)

    def restore_hist(self):

        try:
            with open(join(CONFIG_DIR, 'hist.p'), 'rb') as f:
                self.history = pickle.load(f)

                if type(self.history) is deque:
                    print("migrating from old version...")
                    self.history = list(self.history)

        except (AttributeError, FileNotFoundError, pickle.UnpicklingError):
            print("failed to restore recently played. :(")

    def setup_mpris(self):

        from pydbus import SessionBus, Variant
        from pydbus.generic import signal

        class MPRIS:
            """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <property name="CanQuit" type="b" access="read" />
    <property name="CanRaise" type="b" access="read" />
    <property name="HasTrackList" type="b" access="read" />
    <property name="Identity" type="s" access="read" />
    <property name="SupportedMimeTypes" type="as" access="read" />
    <property name="SupportedUriSchemes" type="as" access="read" />
    <method name="Quit" />
    <method name="Raise" />
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <property name="PlaybackStatus" type="s" access="read" />
    <property name="Rate" type="d" access="readwrite" />
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite" />
    <property name="Position" type="x" access="read" />
    <property name="MinimumRate" type="d" access="readwrite" />
    <property name="MaximumRate" type="d" access="readwrite" />
    <property name="CanGoNext" type="b" access="read" />

    <property name="CanGoPrevious" type="b" access="read" />
    <property name="CanPlay" type="b" access="read" />
    <property name="CanPause" type="b" access="read" />
    <property name="CanSeek" type="b" access="read" />
    <property name="CanControl" type="b" access="read" />

    <method name="Next" />
    <method name="Previous" />
    <method name="Pause" />
    <method name="PlayPause" />
    <method name="Stop" />
    <method name="Play" />
    <method name="Seek">
      <arg type="x" direction="in" />
    </method>
    <method name="SetPosition">
      <arg type="o" direction="in" />
      <arg type="x" direction="in" />
    </method>
    <method name="OpenUri">
      <arg type="s" direction="in" />
    </method>
  </interface>
</node>
            """
            PropertiesChanged = signal()

            def __init__(self, app):
                self.app = app

            def emit_property_changed(self, attr):
                self.PropertiesChanged(
                    "org.mpris.MediaPlayer2.Player",
                    {attr: getattr(self, attr)}, [])

            @property
            def CanQuit(self):
                return False

            @property
            def CanRaise(self):
                return False

            @property
            def HasTrackList(self):
                return False

            @property
            def Identity(self):
                return "TUIJam"

            @property
            def SupportedMimeTypes(self):
                return []

            @property
            def SupportedUriSchemes(self):
                return []

            def Raise(self):
                pass

            def Quit(self):
                pass

            @property
            def PlaybackStatus(self):
                return {
                    'play': 'Playing',
                    'pause': 'Paused',
                    'stop': 'Stopped'
                }[self.app.play_state]

            @property
            def Rate(self):
                return 1.0

            @Rate.setter
            def Rate(self, rate):
                pass

            @property
            def Metadata(self):
                song = self.app.current_song

                if type(song) == Song:

                    logging.info('New song ID: ' + str(song.id))

                    return {
                        'mpris:trackid': Variant('o', '/org/tuijam/GM_'+str(song.id).replace('-', '_')),
                        'mpris:artUrl': Variant('s', song.albumArtRef),
                        'xesam:title': Variant('s', song.title),
                        'xesam:artist': Variant('as', [song.artist]),
                        'xesam:album': Variant('s', song.album),
                        'xesam:url': Variant('s', song.stream_url)
                    }

                elif type(song) == YTVideo:

                    return {
                        'mpris:trackid': Variant('o', '/org/tuijam/YT_'+str(song.id).replace('-', '_')),
                        'mpris:artUrl': Variant('s', song.thumbnail),
                        'xesam:title': Variant('s', song.title),
                        'xesam:artist': Variant('as', [song.channel]),
                        'xesam:album': Variant('s', ''),
                        'xesam:url': Variant('s', song.stream_url)
                    }

                else:
                    return {}

            @property
            def Volume(self):
                return self.app.volume / 8.0

            @Volume.setter
            def Volume(self, volume):
                volume = max(0, min(volume, 1))
                self.app.volume = int(volume*8)
                self.app.player.volume = volume * 100
                self.emit_property_changed("Volume")

            @property
            def Position(self):
                try:
                    return int(1000000 * self.app.player.time_pos)

                except TypeError:
                    return 0

            @property
            def MinimumRate(self):
                return 1.0

            @property
            def MaximumRate(self):
                return 1.0

            @property
            def CanGoNext(self):
                return len(self.app.queue_panel.queue) > 0

            @property
            def CanGoPrevious(self):
                return False

            @property
            def CanPlay(self):
                return len(self.app.queue_panel.queue) > 0 or self.app.current_song is not None

            @property
            def CanPause(self):
                return self.app.current_song is not None

            @property
            def CanSeek(self):
                return self.app.current_song is not None

            @property
            def CanControl(self):
                return True

            def Next(self):
                self.app.queue_panel.play_next()

            def Previous(self):
                pass

            def Pause(self):
                if self.app.play_state == 'play':
                    self.app.toggle_play()

            def PlayPause(self):
                self.app.toggle_play()

            def Stop(self):
                self.app.stop()

            def Play(self, song_id):
                self.app.toggle_play()

            def Seek(self, offset):
                pass

            def SetPosition(self, track_id, position):
                pass

            def OpenUri(self, uri):
                pass

        self.mpris = MPRIS(self)
        bus = SessionBus()
        bus = bus.publish('org.mpris.MediaPlayer2.tuijam',
                          ('/org/mpris/MediaPlayer2', self.mpris))


def lastfm_conf():
    config_file = join(CONFIG_DIR, 'config.yaml')
    if not isfile(config_file):
        print("It seems that you haven't run tuijam yet.")
        print("Please run it first, then authorize to Last.fm.")
        return

    print("generating Last.fm authentication token")
    api = LastFMAPI()
    token = api.get_token()
    auth_url = api.get_auth_url(token)

    import webbrowser
    webbrowser.open_new_tab(auth_url)

    print()
    print("Please open this link in your browser and authorize the app in case the window "
          "hasn't been opened automatically:")
    print(auth_url)
    print()
    input("After that, press Enter to get your session key...")
    if not api.auth_by_token(token):
        print('Failed to get a session key. Have you authorized?')
    else:
        with open(config_file, 'r+') as f:
            lastfm_sk = api.sk
            config = yaml.safe_load(f.read())
            if config.get('encrypted', False):

                from scrypt import decrypt, encrypt
                print('The config is encrypted, encrypting session key...')
                config_pw = getpass("Enter tuijam config pw: ")
                try:
                    decrypt(config['email'], config_pw, maxtime=20)
                    lastfm_sk = encrypt(lastfm_sk, config_pw, maxtime=0.5)
                except Exception as e:
                    print(e)
                    print("Could not decrypt config file.")
                    exit(1)

            config.update({'lastfm_sk': lastfm_sk})
            f.seek(0)
            yaml.safe_dump(config, f, default_flow_style=False)
            f.truncate()
            f.close()
        print('Successfully authenticated.')


def main():

    print("starting up.")
    makedirs(CONFIG_DIR, exist_ok=True)

    log_file = join(CONFIG_DIR, 'log.txt')
    logging.basicConfig(filename=log_file, filemode='w', level=logging.WARNING)

    if 'get_lastfm_token' in sys.argv[1:]:
        lastfm_conf()
        exit(0)

    app = App()
    print("logging in.")
    if not app.login():
        return

    if app.mpris_enabled:
        print("enabling external control.")
        app.setup_mpris()

    if app.persist_queue:
        print("restoring queue")
        app.restore_queue()

    if app.video:
        app.player['vid'] = 'auto'
    app.restore_hist()

    import signal
    signal.signal(signal.SIGINT, app.cleanup)

    loop = urwid.MainLoop(app, palette=app.palette,
                          event_loop=urwid.GLibEventLoop())
    app.loop = loop
    loop.screen.set_terminal_properties(256)

    try:
        loop.run()

    except Exception as e:
        logging.exception(e)
        print("Something bad happened! :( see log file ($HOME/.config/tuijam/log.txt) for more information.")
        app.cleanup()


if __name__ == '__main__':
    main()