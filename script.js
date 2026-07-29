const fs = require('fs');

const dataArray = JSON.parse(fs.readFileSync('/Users/lorenzocastelli/projects/tonyshop/localDecision/data.json', "utf8"));

let data = [];

for (let ix = 0; ix < dataArray.length; ix++) {
    const el = dataArray[ix];

    if (el.toDelete) continue;
    if (el['Prezzo (Tasse Escluse)'] === 0) continue;

    data.push(el);
}

  fs.writeFileSync('/Users/lorenzocastelli/projects/tonyshop/data.json', Buffer.from(JSON.stringify(data)));





