const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Execute the actual inventory-card selection and click code against a minimal DOM.
const source = fs.readFileSync(path.join(__dirname, '../web/static/app.js'), 'utf8');
const start = source.indexOf('        // Farm toggle button');
const end = source.indexOf('        // Ignore toggle', start);
assert(start >= 0 && end > start);
const code = source.slice(start, end);

function card(selected, name = 'Target Game') {
    const excluded = [];
    let clicks;
    const context = {
        state: {settings: {games_to_watch: [...selected]}, campaigns: {
            first: {game_name: 'A Game'}, target: {game_name: 'Target Game'}, last: {game_name: 'Z Game'}
        }},
        campaign: {game_name: name}, inv: {},
        makeElement(tag, attrs, text, build) {
            const el = {tag, attrs, text, children: [], appendChild(child) {this.children.push(child)},
                addEventListener(event, callback) {clicks = callback}};
            if (build) build(el);
            return el;
        },
        setAutoAddExcluded(game, value) {excluded.push([game, value])},
        saveSettings() {}, renderInventory() {},
    };
    vm.createContext(context);
    vm.runInContext(code, context);
    return {
        button: vm.runInContext('farmToggle', context), excluded,
        click() {clicks({stopPropagation() {}})},
        selected() {return Array.from(context.state.settings.games_to_watch)},
    };
}

const empty = card([]);
assert.match(empty.button.attrs.class, /skipped/);
assert.equal(empty.button.children[0].text, '○ Not selected');
empty.click();
assert.deepEqual(empty.selected(), ['Target Game']);
assert.deepEqual(empty.excluded, [['Target Game', false]]);

const last = card(['target game']);
assert.match(last.button.attrs.class, /farming/);
last.click();
assert.deepEqual(last.selected(), []);
assert.deepEqual(last.excluded, [['Target Game', true]]);
assert.match(card(last.selected()).button.attrs.class, /skipped/);

const others = card(['A Game', 'Target Game']);
others.click();
assert.deepEqual(others.selected(), ['A Game']);
console.log('PASS empty means no selection; first click selects only target; last removal stays empty; skip blocks auto-add');
