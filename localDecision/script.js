const fs = require('fs');

const shopifyData = JSON.parse(fs.readFileSync('/Users/lorenzocastelli/projects/tonyshop/files/shopify_products.json', "utf8"));
const vendorData = JSON.parse(fs.readFileSync('/Users/lorenzocastelli/projects/tonyshop/files/vendor_products.json', "utf8"));

let data = [];

for (let ix = 0; ix < vendorData.length; ix++) {
    const el = vendorData[ix];

    const key = el['Riferimento'];

    if (!shopifyData[key]) continue;

    data.push(el);
}

  fs.writeFileSync('/Users/lorenzocastelli/projects/tonyshop/localDecision/data.json', Buffer.from(JSON.stringify(data)));





