#!/bin/bash
# by LiuYang 2020-4-26
# Usage: ginput.sh xxx yyy (xxx is your old input file, and yyy is your output file). Put the ginput.sh file into /usr/bin/ before using. This script can capture the last structural coordinations of gaussian output file to form a new gaussian input file.

# job is started
echo "======================================"
echo

# if the treated file contains freq calculation, some preparations are needed.
if cat $2|grep -i "freq">/dev/null
then
    cp $2 $2.backup
    ln=`cat -n $2.backup|grep -E "Rotational constants" |tail -n 1|awk '{print $1}'`
    sed -i "$ln d" $2
fi

# capture the name of the gaussian output file
name=`echo $2|sed "s/\..*$//"`

# obtain the keywords and parameters from the old gaussian input file
num1=`cat -n $1|grep -E '[0-9][[:space:]][0-9]'|head -1|awk '{print $1}'`
head -n $num1 $1 > temp.gjf

# determine the keywords for obtaining structural coordinations from gaussian output file
if cat $2|grep "Input orientation">/dev/null
then
    echo "Input orientation" > $name.temp1
else
    echo "Standard orientation" > $name.temp1
fi

if cat $2|grep "Distance matrix">/dev/null
then
    echo "Distance matrix" > $name.temp2
else
    echo "Rotational constants" > $name.temp2
fi

# obtain the last structural coordinations from gasussian output file
var1=`cat $name.temp1`
var2=`cat $name.temp2`
num2=`cat -n $2|grep "$var1"|tail -n 1|awk '{print $1}'`
num3=`cat -n $2|grep "$var2"|tail -n 1|awk '{print $1}'`
sed -n "$num2,$num3"p"" $2|grep -v -E "$var1|$var2|Center|Number|\-\-"> $name.temp3
cut -c 37-71 $name.temp3 > $name.temp4

# set some variables
num4=`expr ${num3} - ${num2} - 7`
num5=`expr ${num1} + 1`
num6=`expr ${num4} + ${num5} + 1`

# capture the atom labels, the frozen informations, the layer informations and so on from the old gaussian input file (oniom mode and normal mode)
if cat $2|grep -i "oniom">/dev/null
then
    sed "$num5,+$num4 !d" $1 > $name.temp5
    awk '{print $1}' $name.temp5 > $name.temp6
    awk '{print $2}' $name.temp5 > $name.temp7
    sed -i 's/0/ 0/g' $name.temp7
    awk '{print $6,$7,$8,$9,$10}' $name.temp5 > $name.temp8
    paste $name.temp6 $name.temp7 $name.temp4 $name.temp8 > $name.temp9    # obtain the xyz file 
    sed -i s/[[:space:]]*$//g $name.temp9
    cat $name.temp9 >> temp.gjf        # obtain the new gaussian input file
    sed "$num6,$ !d" $1 >> temp.gjf    # add the connectivity and additional informations
else
    sed "$num5,+$num4 !d" $1|awk '{print $1}'> $name.temp5
    paste $name.temp5 $name.temp4 > $name.temp6
    cat $name.temp6 >> temp.gjf
    sed "$num6,$ !d" $1 >> temp.gjf
fi

# delete the temporary files
rm $name.temp*

# recover the gasussian output file including freq calcualtion
if cat $2|grep -i "freq">/dev/null
then
    mv $2.backup $2
fi

echo "The new gaussian input file is created"
echo
echo "======================================"

