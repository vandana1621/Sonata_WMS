document.addEventListener("DOMContentLoaded",function(){Array.from(document.querySelectorAll(".countdownlist")).forEach(function(c){var t=(new Date).getTime()+15e3,o=new Date(t).getTime(),a=setInterval(function(){var t=(new Date).getTime(),t=o-t,e=Math.floor(t/864e5),i=Math.floor(t%864e5/36e5),n=Math.floor(t%36e5/6e4),s=Math.floor(t%6e4/1e3);c&&(c.innerHTML='<div class="countdownlist-item"><div class="count-title">Days</div><div class="count-num">'+e+'</div></div><div class="countdownlist-item"><div class="count-title">Hours</div><div class="count-num">'+i+'</div></div><div class="countdownlist-item"><div class="count-title">Minutes</div><div class="count-num">'+n+'</div></div><div class="countdownlist-item"><div class="count-title">Seconds</div><div class="count-num">'+s+"</div></div>"),t<0&&(clearInterval(a),document.getElementById("countDownText").innerHTML=`<div class="text-center">
                <img src="assets/images/success-img.png" alt="" height="120">
                <div class="mt-5">
                    <h5 class="text-white">We've Launched our new website</h5>
                    <p class="text-white text-opacity-50">Click the below button to visit our website.</p>
                </div>
                <a href="index.html" class="btn btn-primary icon-link">Back to Home <i class="bi bi-arrow-right"></i></a>
            </div>`)},1e3)})});