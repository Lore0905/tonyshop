const fs = require('fs');

const FILE_NUM = 2;
const PRODOTTI_PATH =  __dirname + `/files/${FILE_NUM}_todo.json`;
const UPDATES_PATH = __dirname + '/file_input.json';

const prodottiArray = JSON.parse(fs.readFileSync(PRODOTTI_PATH, 'utf8'));
const aggiornamenti = JSON.parse(fs.readFileSync(UPDATES_PATH, 'utf8'));



function updateProducts(products, updates) {
    return products.map(product => {
        const id = product["Codice prodotto"];

        // cerco l'aggiornamento per codice prodotto
        const update = updates[id];

        // se non esiste lascio invariato
        if (!update) {
            return product;
        }

        return {
            ...product,

            // aggiorno campi esistenti
            "Nome": update.nome,
            "Descrizione": update.descrizione,

            // aggiungo nuovi campi
            "meta_title": update.meta_title,
            "meta_description": update.meta_description,
            "keywords": update.keywords
        };
    });
}


// esempio utilizzo

const updatedProducts = updateProducts(
    prodottiArray,
    aggiornamenti
);

console.log(`Aggiornamento di elementi ${updatedProducts.length}`);
console.log(`Path ${PRODOTTI_PATH}`);

fs.writeFileSync(PRODOTTI_PATH, JSON.stringify(updatedProducts, null, 2), 'utf8');