const MAX_VIDEO_RESULTS = 25;
const YOUTUBE_CHANNEL_URL = 'https://www.youtube.com/user/';
const LASTFM_API_URL = 'http://ws.audioscrobbler.com/2.0/';
const LASTFM_API_KEY = 'c2933b27a78e04c4b094a1a094bc2c9c';

const noContentParagraph = document.getElementById('no-content-message');
const lastWeekFilter = document.getElementById('last-week-filter');
const lastMonthFilter = document.getElementById('last-month-filter');
const lastYearFilter = document.getElementById('last-year-filter');
const eternityFilter = document.getElementById('eternity-filter');
const subscriptionSelect = document.getElementById('sub-select');
const global_subscriptions = {};
let modifyingSubscriptions = false;
let jsonpCounter = 0;


function hideElement(element) {
    element.style.display = 'none';
}

function showElement(element) {
    element.style.display = 'block';
}

function queryString(params) {
    let string = '?';
    for (let [key, entry] of Object.entries(params))
        string += `${encodeURIComponent(key)}=${encodeURIComponent(entry)}&`;
    return string;
}


function fetchJSONP(url, params) {
    return new Promise((resolve, reject) => {
        let callbackName = 'jsonp' + new Date().getTime() + jsonpCounter;

        jsonpCounter += 1;
        params['callback'] = callbackName;

       // TODO reject bad jsondata
       window[callbackName] = jsonData => {
           resolve(jsonData);
       };

       let script = document.createElement('script');
       script.src = url + queryString(params);
       document.getElementsByTagName('head')[0].appendChild(script);
    });
}

// TODO put this at the subscribe/unsubscribe functions instead of closures around their usages
// TODO fix nesting of lock decorators ?
const lockDecorator = (wrapped) => {
    const decorated = (...args) => {
        if (modifyingSubscriptions) {
            throw ("already modifying subscriptions");
        } else {
            modifyingSubscriptions = true;
            try {
                wrapped(...args);
            } finally {
                modifyingSubscriptions = false;
            }
        }
    };

    return decorated;
};


const asyncLockDecorator = (wrapped) => {
    const decorated = (...args) => {
        let promise;
        if (modifyingSubscriptions) {
            promise = Promise.reject("already modifying subscriptions.");
        } else {
            modifyingSubscriptions = true;
            promise = wrapped(...args);
            promise.then(unlock, unlock);
        }
        return promise;
    };

    const unlock = () => {
        modifyingSubscriptions = false;
    };

    return decorated;
};


// TODO differentiate between messages that must be seen from an user and from a developer


class SystemError extends Error {
    constructor (message) {
        super(message);
    }
}

class ApplicationError extends Error {
    constructor (message) {
        super(message);
    }
}

class ExternalError extends Error {
    constructor (message) {
        super(message);
    }
}

class PeerError extends ExternalError {
    constructor (message) {
        super(message);
    }
}

class UserError extends ExternalError {
    constructor (message) {
        super(message);
    }
}

async function getSubscription (url) {
    async function fetchChannel (url) {
        let channelInformation = {};
        let identifiers = parseChannelFromYTUrl(url);
        let params = {};

        channelInformation.originalUrl = url;

        if (identifiers.hasOwnProperty('id') && identifiers.id.length !== 0) {
            params = {
                part: 'snippet, contentDetails',
                id: identifiers.id,
            };
        } else if (identifiers.hasOwnProperty('name') &&
            identifiers.name.length !== 0) {
            params = {
                part: 'snippet, contentDetails',
                forUsername: identifiers.name,
            };
        } else {
            throw new UserError(
                `Youtube url - ${url} is not a link to a channel.`);
        }

        if (!hasChainedProperties(['client.youtube.channels.list'], gapi)) {
            throw new PeerError(
                `Youtube API is not behaving correctly. Missing gapi.client.youtube.channels.list`);
        }

        let response;

        try {
            response = await gapi.client.youtube.channels.list(params);
        } catch (reason) {
            throw new PeerError('Youtube API failed to provide information about channel. Reason: ' +
                reason);
        }

        if (
            !hasChainedProperties(['result.items'], response) &&
            !hasChainedProperties([
                    'snippet.title',
                    'snippet.customUrl',
                    'id',
                    'contentDetails.relatedPlaylists.upload'],
                response.result.items[0])
        ) {
            throw new PeerError(
                'Youtube API didn\'t properly provide information about channel');
        }

        let item = response.result.items[0];

        channelInformation.id = item.id;
        channelInformation.title = item.snippet.title;
        channelInformation.customUrl = item.snippet.customUrl;
        channelInformation.playlistId = item.contentDetails.relatedPlaylists.uploads;

        return channelInformation;
    }

    async function fetchTracks (playlistId) {
        if (!hasChainedProperties(['playlistItems.list'], gapi.client.youtube))
            throw new PeerError(
                `Youtube API failed to provide information about channel 
                uploads. Reason: renamed properties of gapi.client.youtube`
            );

        let response;

        try {
            response = await gapi.client.youtube.playlistItems.list({
                maxResults: MAX_VIDEO_RESULTS,
                part: 'snippet,contentDetails',
                playlistId: playlistId,
            });
        } catch (reason) {
            console.log('Youtube API failed. Reason', reason);

            throw new PeerError(
                `Youtube API failed to provide information 
                about channel uploads. Reason: API call failed`
            );
        }

        let tracks = [];
        let failures = [];

        for (let item of response.result.items) {
            if (!hasChainedProperties(['snippet.title', 'snippet.publishedAt'], item)) {
                throw new PeerError(
                    `Youtube API failed to provide information about channel uploads. 
                    Reason: renamed properties of response.result.items`
                );
            }

            let parsed = parseTrackFromVideoTitle(item.snippet.title);

            if (parsed.artist && parsed.track) {
                tracks.push(
                    new Track({
                        artist: parsed.artist,
                        title: parsed.track,
                        featuring: parsed.feat,
                        publishedAt: item.snippet.publishedAt,
                    }),
                );
            } else {
                failures.push(item.snippet.title);
            }
        }

        return {tracks, failures};
    }

    // async function fetchTrackInfo (tracks) {
    //     let failures = {};
    //     for (let track of tracks) {
    //         track.loadData().
    //             then(response => {}).
    //             catch(reason => { failures[track.name] = reason; });
    //     }
    //     return {tracks: tracks, failures};
    // }

    let subscription = getSubByFullUrl(url);

    if (subscription) {
        console.log('found existing sub', subscription);
        return subscription;
    }

    // let errors propagate to the caller.
    subscription = await fetchChannel(url);
    let trackResponse = await fetchTracks(subscription.playlistId);

    subscription.tracks = trackResponse.tracks;
    subscription.failedToParse = trackResponse.failures;
    subscription.filterFunc = (track => true);
    subscription.filterElement = eternityFilter;

    // let {tracks, failures} = await fetchTrackInfo(subscription.tracks);
    //
    // subscription.tracks = tracks;
    // subscription.failedToLoad = failures;

    return subscription;
}

function getSubByCustomurl (customUrl) {
    customUrl = customUrl.toLowerCase();

    for (let sub of Object.values(global_subscriptions)) {
        if (sub.customUrl.toLowerCase() === customUrl) {
            return sub;
        }
    }
}

function getSubByFullUrl (url) {
    let parsed = parseChannelFromYTUrl(url);

    if (parsed.id && global_subscriptions[parsed.id]) {
        return global_subscriptions[parsed.id];
    } else if (parsed.name) {
        let sub = getSubByCustomurl(parsed.name);

        if (sub) {
            return sub;
        }
    }
}

class Track {
    constructor ({artist, title, featuring, publishedAt}) {
        // url, duration, artistUrl, album properties are loaded and attached through the loadData async method
        this.id = artist + title;
        this.name = title;
        this.artistName = artist;
        this.featuring = featuring;
        this.publishedAt = new Date(publishedAt);
    }

    trackInfoUrlParams () {
        return {
            method: 'track.getInfo',
            format: 'json',
            api_key: LASTFM_API_KEY,
            artist: this.artistName,
            track: this.name,
        };
    }

    async loadData () {
        console.log("loading data");
        let response;

        try {
            response = await fetchJSONP(LASTFM_API_URL, this.trackInfoUrlParams());
        } catch (error) {
            throw PeerError("there was a problem with the LastFM api for track " + this.name);
        }
        console.log("response", response);

        if (!response.track)
            throw new PeerError('last.fm api doesn\'t have a track property in it\'s response for track: ' +
                this.name);

        this.name = response.track.name;
        this.duration = response.track.duration;
        this.url = response.track.url;
        this.artistUrl = response.track.artist.url;
        this.artistImageUrl = '';
        this.album = response.track.album;
    }
}

function hasChainedProperties (chainedProperties, object) {
    for (let chainedProp of chainedProperties) {
        let properties = chainedProp.split('.');
        let chainedObject = object;

        for (let prop of properties) {
            if (chainedObject[prop] === undefined)
                return false;

            chainedObject = chainedObject[prop];
        }
    }

    return true;
}

function getProp (chainedProp, object, default_) {
    for (let prop of chainedProp.split('.')) {
        if (object[prop]) {
            object = object[prop];
        } else {
            return default_;
        }
    }

    return object;
}

async function subscribe (url) {
    let sub = await getSubscription(url);

    // it's possible for a race condition to occur where
    // getSubscription returns twice two different subscriptions
    // to the same channel, because different urls can point to the same channel.
    if (global_subscriptions[sub.id]) {
        return;
    }

    global_subscriptions[sub.id] = sub;
    let option = document.createElement('option');
    option.value = url;
    option.innerHTML = sub.title;
    subscriptionSelect.appendChild(option);
    UrlList.display(sub);

    for (let track of sub.tracks) {
        try {
            await track.loadData();
        } catch (error) {
            console.warn(error);
            continue;
        }

        TableAPI.displayTrack(sub, track);
        TableAPI.showTable();
    }
}

function unsubscribe (url) {
    let sub = getSubByFullUrl(url);

    for (let k=0; k < subscriptionSelect.children.length; k++) {
        let option = subscriptionSelect.children[k];

        if (option.value.toLowerCase() === url.toLowerCase()) {
            option.remove();
            break;
        }
    }

    if (sub) {
        delete global_subscriptions[sub.id];
        UrlList.hide(sub);
    } else {
        alert('Already unsubscribed from ' + url);
    }

    TableAPI.removeSubscription(sub);
}

function clearSubs () {
    console.log("clearing subs");
    TableAPI.clearTable();
    for (let [key, sub] of Object.entries(global_subscriptions)) {
        delete global_subscriptions[key];
        UrlList.hide(sub);
    }

    while(subscriptionSelect.firstChild) {
        subscriptionSelect.removeChild(subscriptionSelect.firstChild);
    }
}

function parseUrlsFromString (str) {
    // taken from https://stackoverflow.com/questions/6038061/regular-expression-to-find-urls-within-a-string
    let regex = new RegExp(
        '(http|ftp|https)://([\\w_-]+(?:(?:\\.[\\w_-]+)+))([\\w.,@?^=%&:/~+#-]*[\\w@?^=%&/~+#-])?',
        'g');
    let urls = [];
    let match = regex.exec(str);

    while (match) {
        urls.push(match[0]);
        match = regex.exec(str);
    }

    return urls;
}

function parseChannelFromYTUrl (url) {
    let idResult = new RegExp('www.youtube.com/channel/([^/]+)').exec(url); // TODO parses query params incorrectly
    let userResult = new RegExp('www.youtube.com/user/([^/]+)').exec(url);

    if (idResult) {
        return {
            id: idResult[1],
            type: 'id',
        };
    } else if (userResult) {
        return {
            name: userResult[1],
            type: 'username',
        };
    } else {
        return {};
    }
}

function parseTrackFromVideoTitle (videoTitle) {
    let regex = /\s*([^_]+)\s+-\s+([^()]+)\s*/;
    let featuringRegex = /(?:ft\.|feat\.)\s*(.*)/;
    let featMatch = featuringRegex.exec(videoTitle);
    let feat;

    if (featMatch) {
        videoTitle = videoTitle.replace(featMatch[0], '');
        feat = featMatch[1];
    }

    let result = regex.exec(videoTitle);

    if (!result) {
        console.log('Could not parse title ' + videoTitle);
        return {};
    }

    result = result.map(ele => {
        if (ele)
            return ele.trim();

        return ele;
    });

    return {artist: result[1], track: result[2], feat};
}

const TableAPI = {
    table: document.getElementsByClassName('table')[0],
    tableBody: document.querySelector('table tbody'),
    tableItemTemplate: document.getElementById('table-row-template'),
    rowElements: {},

    prepareChannelDialog: (dialog, sub) => {
        dialog.getElementsByTagName('p')[0]
            .textContent = sub.title;
        dialog.getElementsByTagName('a')[0]
            .setAttribute('href', YOUTUBE_CHANNEL_URL + sub.customUrl);
    },

    prepareArtistDialog: (dialog, track) => {
        dialog.getElementsByTagName('p')[0]
            .textContent = track.artistName + "'s last.fm page";
        dialog.getElementsByTagName('a')[0]
            .setAttribute('href', track.artistUrl);
        dialog.getElementsByTagName('img')[0]
            .setAttribute('src', track.artistImageUrl)
    },

    isTrackDisplayed: track => !!TableAPI.rowElements[track.id],

    displayTrack: (sub, track) => {
        if (TableAPI.isTrackDisplayed(track)) {
            console.warn(
                'Track is already displayed but tried to display it again. Track: ',
                track);
            return;
        } else if(!sub.filterFunc(track)) {
            return;
        }

        let newRow = TableAPI.tableItemTemplate.cloneNode(true);
        let title = getProp('title', sub, '');
        let artistName = getProp('artistName', track, '');
        let albumTitle = getProp('album.title', track, '');
        let trackName = getProp('name', track);
        let duration = getProp('duration', track, 0);

        newRow.classList.remove('hidden');
        newRow.removeAttribute('id');
        duration = (duration / 1000 / 60).toFixed(2).
            replace('.', ':').
            padStart(5, '0');

        const rowData = [
            title,
            artistName,
            albumTitle,
            trackName,
            duration,
        ];

        for (let k = 0; k < newRow.cells.length; k++) {
            newRow.cells[k].appendChild(
                document.createTextNode(rowData[k]),
            );
        }

        let dialog = newRow.cells[0].getElementsByTagName('dialog')[0];

        TableAPI.prepareChannelDialog(dialog, sub);
        TableAPI.prepareArtistDialog(newRow.cells[1].getElementsByTagName('dialog')[0], track);

        function dialogHook() {
            let dialogs = this.getElementsByTagName('dialog');

            if (dialogs === undefined || dialogs.length === 0)
                throw new ApplicationError("Couldn't find dialog element for " + this);

            // does not work in firefox 60.0.2 for Ubunutu
            if(dialog.showModal === undefined) {
                throw new SystemError("Dialog's are not supported in your browser");
            }
            dialog.showModal();
        }
        for(let k=0; k<2; k++) {
            newRow.cells[k].addEventListener('click', dialogHook);
        }

        TableAPI.rowElements[track.id] = newRow;
        TableAPI.tableBody.appendChild(newRow);
    },

    removeTrackById: id => {
        if (!TableAPI.rowElements[id]) {
            console.warn(
                'Tried to hide a track that is already hidden. Track id: ', id);

            return;
        }

        TableAPI.rowElements[id].remove();
        delete TableAPI.rowElements[id];
    },

    removeSubscription: sub => {
        for (let track of sub.tracks) {
            TableAPI.removeTrackById(track.id);
        }
    },

    showTable: () => {
        TableAPI.table.classList.remove('hidden');
        noContentParagraph.classList.add('hidden');
    },

    hideTable: () => {
        TableAPI.table.classList.add('hidden');
        noContentParagraph.classList.remove('hidden');
        noContentParagraph.textContent = '';
    },

    clearTable: () => {
        for (let trackId of Object.keys(TableAPI.rowElements))
            TableAPI.removeTrackById(trackId);

        TableAPI.hideTable();
    },

};

const UrlList = {

    listElements: new WeakMap(),

    isDisplayed: sub => UrlList.listElements.has(sub),

    newItem: sub => {
        let itemTemplate = document.getElementById('url-list-item-template');
        let newItem = itemTemplate.cloneNode(true);

        newItem.childNodes[0].nodeValue = sub.title;
        newItem.childNodes[1].addEventListener('click',
            lockDecorator(() => {
                unsubscribe(sub.originalUrl);
            }
        ));

        newItem.classList.remove('hidden');
        newItem.removeAttribute('id');

        return newItem;
    },

    display: sub => {
        if (UrlList.isDisplayed(sub))
            return;

        let listItem = UrlList.newItem(sub);

        document.getElementById('url-list').appendChild(listItem);
        UrlList.listElements.set(sub, listItem);
    },

    hide: sub => {
        UrlList.listElements.get(sub).remove();
        UrlList.listElements.delete(sub);
    },
};

function dateFilter(until) {
    return track => track.publishedAt.getTime() <= until;
}

function songIsInDateRange (song) {
    let songDate = song.publishedAt;
    let dateRange = new Date();

    if (lastWeekFilter.checked) {
        dateRange.setDate(dateRange.getDate() - 7);
    } else if (lastMonthFilter.checked) {
        dateRange.setMonth(dateRange.getMonth() - 1);
    } else if (lastYearFilter.checked) {
        dateRange.setFullYear(dateRange.getFullYear() - 1);
    } else {
        // TODO assert
        return false;
    }

    return songDate.getTime() <= dateRange.getTime();
}

function setupFiltering() {
    const lastWeek = new Date();
    const lastMonth = new Date();
    const lastYear = new Date();

    lastWeek.setDate(lastWeek.getDate() - 7);
    lastMonth.setMonth(lastMonth.getMonth() - 1);
    lastYear.setFullYear(lastYear.getFullYear() - 1);

    filterFuncHash = {
        'last-week-filter': dateFilter(lastWeek),
        'last-month-filter': dateFilter(lastMonth),
        'last-year-filter': dateFilter(lastYear),
        'eternity-filter': (track => true)
    };

    function selectedFilter (event) {
        if(!subscriptionSelect.options || subscriptionSelect.options.length === 0)
            return;

        let sub_url = subscriptionSelect.options[subscriptionSelect.selectedIndex].value;
        let sub = getSubByFullUrl(sub_url);
        sub.filterFunc = filterFuncHash[event.target.getAttribute('id')];
        sub.filterElement = event.target;
    }
    function selectedSubscription (event) {
        if(!subscriptionSelect.options || subscriptionSelect.options.length === 0)
            return;

        let sub_url = subscriptionSelect.options[subscriptionSelect.selectedIndex].value;
        let sub = getSubByFullUrl(sub_url);
        if(sub.filterElement !== undefined) {
            sub.filterElement.checked = true;
        }
    }

    subscriptionSelect.addEventListener('change', selectedSubscription);
    lastWeekFilter.addEventListener('click', selectedFilter);
    lastMonthFilter.addEventListener('click', selectedFilter);
    lastYearFilter.addEventListener('click', selectedFilter);
    eternityFilter.addEventListener('click', selectedFilter);
}


function setupSubEvents (form, button) {
    const submitUrls = asyncLockDecorator(async () => {
        let urls = parseUrlsFromString(
            document.getElementById('urls-input').value);
        let promises = urls.map(subscribe);

        await Promise.all(promises);
    });

    async function processForm (e) {
        if (e.preventDefault)
            e.preventDefault();

        await submitUrls();

        // return false to prevent the default form behavior
        return false;
    }

    if (form.attachEvent) {
        form.attachEvent('submit', processForm);
    } else {
        form.addEventListener('submit', processForm);
    }

    button.addEventListener('click', lockDecorator(clearSubs));
}

function start () {
    // Initializes the client with the API key and the Translate API.
    gapi.client.init({
        'apiKey': 'AIzaSyAHhFtmNEo9TwEN90p6yyZg43_4MKCiyyQ',
        'discoveryDocs': ['https://www.googleapis.com/discovery/v1/apis/translate/v2/rest'],
    });

    gapi.client.load('youtube', 'v3', function () {
        console.log('youtube loaded');
    });
}

window.onload = function () {
    gapi.load('client', start);

    let form = document.getElementById('url-form');
    let button = document.getElementById('unsubscribe-all-btn');

    setupSubEvents(form, button);
    setupFiltering();

};
