"""Common English words, for telling a place's own name from words anyone says (sizeup/geo/pagegeo.py): a department
place name made only of these ("Service Station", "Water Tower", "Senior Center", "Auto Body", "Grand Apartments")
names nothing on its own and needs its town said beside it. Lower case, single words; the street types and the page
locator's GENERIC words count too."""

COMMON_WORDS = frozenset("""
able about above across act action active adult advance aero after again age agency air airport all alley allied
alpha alt alternative am america american ammo an and angel angels animal annex answer antique antiques any apartment
apple appliance apply arch area arena arm arms army around art arts as ash assembly assist associates association at
athletic atlantic auction auto automotive autos avenue away baby back bagel bagels bake baker bakery ball band bank
banquet bar barber barn base basic basin bath bay beach bean bear beauty bed beef beer bell belt bench benefit berry
best better beverage big bike bikes bill bird black blind block blood bloom blossom blue board boat boats body
boiler bond book books boost boston bottle boulevard bowl bowling box boy boys branch brand brass bread breakfast brick
bridal bridge bright bristol broad brother brothers brown budget build builder builders building burger burgers bus
business butcher buy by cable cafe cake cakes camp candy canine capital car card care career cargo carpet cars carwash
case cash castle cat catering cell cellar center central centre century chair chamber champion change chapter charter
check cheese chef chemical chicken child children chinese choice christian circle city civic class classic clean
cleaner cleaners cleaning clear clinic clock closet cloth clothing cloud club coach coast coffee cold collection
college colonial color comfort commerce commercial common communications community company complete computer concrete
condo condominium condos connection construction consulting contractor control convenience cook cool copy corner
corporate corporation cottage council counseling country county court courts cove craft crafts cream creative creek
crest cross crossing crown cruise cup cut cuts daily dairy dance data day daycare deal dealer deck deli delivery dental
dentist depot design detail development diamond digital diner direct discount district dock doctor dog dogs dollar
door doors double down downtown dream dress drive driving drop drug drugs dry eagle early earth east easy eat eats
eco edge education electric electronics elite emergency empire energy engine engineering enterprise enterprises
entertainment equipment estate estates euro event events exchange executive exit expert experts express extra eye
eyes fabric factory fair faith family famous farm farms fashion fast federal fellowship fence field fifth fine finish
fire first fish fishing fit fitness five flag flat fleet floor floors flower flowers fly food foods foot for force
forest fort forum foundation four fourth fox frame free fresh friend friends from front fruit fuel full fun fund
funeral furniture gallery game games garage garden gardens gas gate gateway general gift gifts girl girls glass global
go gold golden golf good gourmet grace grand graphics grass gray great green grill grocery ground group grove growth
guard guest guide gym hair hall hand happy harbor harbour hardware harvest haven head health heart heating heights
help heritage hidden high hill hills hobby holiday holy home homes hope horizon horse hospital host hot hotel hour
house housing hub human hunt ice image imports in income independent india indoor industrial industries info inn
inner insurance international investment iron island italian jewel jewelers jewelry joint junior just kid kids king
kitchen knight lab labs lake land landing landmark landscape landscaping lane language large last laundromat laundry
law lawn lead learning leasing legal leisure liberty life light lighting lime limo limousine line link liquor liquors
little live lock locksmith lodge loft long lot lounge love lower lucky lumber machine magic mail main maintenance
majestic mall management manor manufacturing maple marine mark market marketing mart master masters meadow meat
medical medicine meeting memorial men mens metal metro middle mile mill mini mission mobile money motel motor
motors mountain move moving muffler multi municipal museum music mutual nail nails nation national natural nature
navy neck new news night nine north northern now nursery nursing oak ocean office offices oil old olive one online
open optical orchard order organic outdoor outlet over pack paint painting palace palm pantry paper park parking parts
party pass pastry path pawn peace pearl pediatric pediatrics people perfect performance pet pets pharmacy phone photo
physical pier pilot pine pizza pizzeria place plaza plumbing plus pocket point police pond pool port post power
premier premium press prime print printing private pro professional professionals program project property protection
public pump pure quality queen quick rail railroad rain ranch rapid ready real realty recovery recreation red regional
rehab rehabilitation rent rental repair reserve residence residences resort resource resources rest restaurant
restaurants retail ridge right ring river road rock rose route royal rug run safe safety sail sailing salon sales
salt sand savings school science sea seafood seaside season second security senior service services seven shade
shell shelter ship shipping shoe shoes shop shopping shore short side sign signs silver sister sisters six sky small
smart smoke snow soccer social soft solutions sound south southern space spa special specialty speed spirit sport
sports spot spring square stadium staff stage star start state station steak steel stone stop storage store stores
storm street studio studios style suite summer sun sunrise sunset super supermarket superior supply support supreme
surgery surgical sweet swim system systems table tailor tan tavern tax taxi tea team tech technology temple ten
tennis terminal terrace texas thai the theater theatre therapy thrift tile time tire tires title today tool tools top
total tower towers town toy toys track trade trading trail train training transit transport transportation travel
tree trees tri triangle trinity trust truck trucks tutor twin two union united unity universal university upper
urban used valley value van variety vet veterinary video view village vineyard vintage vision vista wall walk war
warehouse wash watch water wave way web wellness west western wheel white whole wholesale wild willow window windows
wine wines winter wireless wise women womens wood woods work works world yacht yard yellow yoga young youth zone
""".split())
