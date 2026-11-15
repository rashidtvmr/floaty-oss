const test = require('node:test');
const assert = require('node:assert/strict');
const v = require('../security-lab/capture-verification.js');
test('rejects bad frames', () => assert.throws(() => v.detectMarker(null), TypeError));
function frame(visible){
 const width=300,height=220,data=new Uint8ClampedArray(width*height*4).fill(255);
 if(visible){const bands=[[237,10,169],[9,203,232],[246,215,43]];
  for(let y=24;y<144;y++)for(let x=30;x<180;x++){
   const i=(y*width+x)*4,c=bands[Math.floor((y-24)/40)];
   data[i]=c[0];data[i+1]=c[1];data[i+2]=c[2];}}
 return {width,height,data};
}
test('positive and negative capture markers',()=>{
 assert.equal(v.detectMarker(frame(true)).detected,true);
 assert.equal(v.detectMarker(frame(false)).detected,false);
});
