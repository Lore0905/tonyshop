const fs = require('fs');

// CONSTANT
const INPUT_PATH = __dirname + '/tag_output.json';


function getUniqueTags() {
    const array = JSON.parse(fs.readFileSync(INPUT_PATH, "utf8"));

    const tags = new Set();

    for (const el of array) {
        tags.add(el.tag);
    }

    // Array ordinato
    const sortedTags = [...tags].sort((a, b) => a.localeCompare(b));

    console.log(sortedTags);

    // Se ti serve ancora un Set ordinato
    const sortedSet = new Set(sortedTags);

    console.log(sortedSet);
}




getUniqueTags();